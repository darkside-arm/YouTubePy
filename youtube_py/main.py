"""YouTube port para R36S/R36T - reescritura en Python + PySDL2.
UI estilo original: sidebar, grid 2x2 de thumbnails, barra de ayuda."""
import collections
import ctypes
import json
import os
import subprocess
import sys
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("PYSDL2_DLL_PATH", "system")

import sdl2
import sdl2.sdlttf as ttf
import sdl2.sdlimage as img
try:
    import sdl2.sdlgfx as gfx
except Exception:
    gfx = None

import backend

CFG = json.load(open(os.path.join(BASE, "config.json")))
LW, LH = CFG["ui"]["logical_w"], CFG["ui"]["logical_h"]


def _auto_margin():
    """R36T/K36S (bisel tipo TV que tapa los bordes) -> 35 px; resto -> 8."""
    try:
        with open("/proc/device-tree/model", "rb") as f:
            model = f.read().decode(errors="replace")
    except OSError:
        model = ""
    return 35 if ("R36T" in model or "K36S" in model) else 8


def _margin(axis):
    v = CFG["ui"].get(axis, "auto")
    return _auto_margin() if v == "auto" else int(v)


# mpv es el unico reproductor soportado. Se probaron dos alternativas y las
# dos se descartaron:
#   - ffplay: no admite pista de audio separada (DASH sin sonido) y no expone
#     control del buffer de red.
#   - vlc/cvlc: se niega a correr como root (el launcher usa sudo), su unica
#     salida de video es el framebuffer y decodifica por software; incluso
#     degradando privilegios el resultado va a tirones.
# Si la consola no trae mpv, el port se lo descarga (ver _install_mpv).
PLAYERS = ("mpv",)

# Directorios donde buscar el binario. BASE/bin va primero por si el port
# trae su propio mpv empaquetado.
PLAYER_DIRS = (os.path.join(BASE, "bin"), "/usr/bin", "/usr/local/bin", "/bin")


def _find_exe(name):
    for d in PLAYER_DIRS:
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


def _which_player():
    """Primer reproductor disponible."""
    for name in CFG.get("video", {}).get("players", PLAYERS):
        path = _find_exe(name)
        if path:
            return name, path
    return None, None


def _player_env():
    """Entorno para el reproductor.

    Algunas imagenes (DarkOS/ArkOS) sustituyen libgbm.so.1 por un symlink a
    libMali.so, que no exporta gbm_surface_create_with_modifiers y hace que
    mpv aborte nada mas arrancar. Si el port trae su propia copia de la
    libgbm de mesa en youtube_py/lib, se antepone solo para el reproductor;
    el resto del sistema sigue usando la de Mali."""
    env = dict(os.environ)
    libdir = os.path.join(BASE, "lib")
    if os.path.isdir(libdir):
        prev = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = libdir + (":" + prev if prev else "")
    return env


# Modos de encaje del video. La pantalla es 4:3 (640x480) y casi todo
# YouTube es 16:9, asi que algo hay que sacrificar. Medido en la R36S con un
# 360p: sin escalado 80% de CPU, con recorte a pantalla completa 120-138%.
# Por eso "fit" es el primero: ademas de no cortar nada, es el mas barato.
ASPECT_MODES = [
    ("aspect_fit", "aspect_fit_hint",
     ["--keepaspect=yes", "--panscan=0.0"]),
    ("aspect_fill", "aspect_fill_hint",
     ["--keepaspect=yes", "--panscan=1.0"]),
    ("aspect_stretch", "aspect_stretch_hint",
     ["--keepaspect=no"]),
]
# Comandos equivalentes para cambiarlo en caliente por IPC.
ASPECT_IPC = [
    [["set", "keepaspect", "yes"], ["set", "panscan", "0"]],
    [["set", "keepaspect", "yes"], ["set", "panscan", "1"]],
    [["set", "keepaspect", "no"]],
]
# Aviso en pantalla al cambiar de modo. mpv expande ${...} con sus propias
# propiedades, asi que el tamano real del video lo pone el.
ASPECT_OSD_MS = 1800
# El socket va en /tmp (tmpfs): /roms suele ser exFAT y no admite sockets.
MPV_SOCKET = "/tmp/youtubepy-mpv.sock"


def _vlog(msg):
    """Traza a log.txt. Durante el video no hay UI donde mostrar nada, asi
    que sin esto es imposible saber si un boton llego a registrarse."""
    sys.stderr.write("video: %s\n" % msg)
    sys.stderr.flush()


def _osd_setup_ipc():
    """Mismos ajustes que _osd_args pero por IPC.

    Se reenvian con cada aviso: asi el texto sale centrado y en amarillo
    aunque mpv se hubiera lanzado sin esos parametros (por ejemplo tras
    actualizar el codigo con un video ya en marcha)."""
    _, h = _screen_size()
    return [
        ["set", "osd-align-x", "center"],
        ["set", "osd-align-y", "center"],
        ["set", "osd-scale-by-window", "no"],
        ["set", "osd-font-size", str(max(20, h // 12))],
        ["set", "osd-color", "#FFFF00"],
        ["set", "osd-border-color", "#000000"],
        ["set", "osd-border-size", "3"],
    ]


def _mpv_query(props, timeout=1.0):
    """Lee propiedades de mpv por IPC. Devuelve dict {prop: valor}.

    Hace falta porque show-text NO expande ${...} cuando el comando llega por
    el socket: el texto sale literal. Asi que los valores se piden antes y se
    interpolan aqui."""
    import socket
    import time
    out = {}
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(MPV_SOCKET)
        for i, p in enumerate(props):
            s.sendall((json.dumps({"command": ["get_property", p],
                                   "request_id": i}) + "\n").encode())
        time.sleep(0.25)
        data = s.recv(65536).decode(errors="replace")
        s.close()
        for line in data.splitlines():
            try:
                j = json.loads(line)
            except ValueError:
                continue
            rid = j.get("request_id")
            if isinstance(rid, int) and rid < len(props):
                out[props[rid]] = j.get("data")
    except OSError:
        pass
    return out


def _ratio_text(w, h):
    """'854x480 - 16:9'. Sin la relacion no se entiende por que un modo no
    cambia nada: en un video 4:3 los tres se ven igual."""
    if not w or not h:
        return ""
    try:
        from fractions import Fraction
        f = Fraction(int(w), int(h)).limit_denominator(30)
        return "%dx%d - %d:%d" % (w, h, f.numerator, f.denominator)
    except (ValueError, ZeroDivisionError):
        return "%sx%s" % (w, h)


def _mpv_ipc(commands, retries=3):
    """Envia comandos a mpv por su socket IPC.

    mpv crea el socket un poco despues de arrancar, y el primer intento puede
    llegar antes de tiempo; de ahi los reintentos. Devuelve True si se
    enviaron. Los fallos se anotan en el log, que si no era imposible saber
    por que un boton "no hacia nada"."""
    import socket
    import time
    for attempt in range(retries):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(MPV_SOCKET)
            for i, c in enumerate(commands):
                s.sendall((json.dumps({"command": c,
                                       "request_id": i}) + "\n").encode())
            # NO cerrar sin mas: mpv procesa el socket de forma asincrona y
            # cerrarlo justo despues del sendall descarta lo pendiente. Hay
            # que esperar a que conteste, que ademas confirma que se ejecuto.
            ok = _wait_replies(s, len(commands))
            s.close()
            if ok:
                return True
        except OSError as e:
            if attempt == retries - 1:
                sys.stderr.write("mpv ipc: %s (%s)\n" % (e, MPV_SOCKET))
                sys.stderr.flush()
        time.sleep(0.15)
    return False


def _wait_replies(sock, n, timeout=1.0):
    """Espera las respuestas de mpv a n comandos. True si llegaron todas."""
    import time
    seen = set()
    buf = ""
    end = time.time() + timeout
    while time.time() < end and len(seen) < n:
        try:
            chunk = sock.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk.decode(errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            rid = j.get("request_id")
            if rid is None:
                continue          # evento, no respuesta
            seen.add(rid)
            if j.get("error") not in (None, "success"):
                sys.stderr.write("mpv ipc: comando %s -> %s\n"
                                 % (rid, j.get("error")))
                sys.stderr.flush()
    return len(seen) >= n


def _screen_size():
    """Resolucion real de la pantalla, leida del framebuffer.

    Se usa para dimensionar el OSD de mpv. Si no se puede leer, se cae a la
    resolucion logica de la UI."""
    try:
        with open("/sys/class/graphics/fb0/virtual_size") as f:
            w, h = f.read().strip().split(",")
            return int(w), int(h)
    except (OSError, ValueError):
        return LW, LH


def _osd_args():
    """OSD de mpv: centrado, amarillo y proporcional a la pantalla.

    Va CENTRADO a proposito: por defecto mpv lo pinta en la esquina superior
    izquierda, que en estas consolas queda tapada por el bisel de la carcasa.
    El tamano se calcula desde la altura real (1/12 de la pantalla) y se
    desactiva el escalado propio de mpv, que toma 720p como referencia y
    dejaria la letra a dos tercios en un panel de 480."""
    _, h = _screen_size()
    return [
        "--osd-align-x=center",
        "--osd-align-y=center",
        "--osd-scale-by-window=no",
        "--osd-font-size=%d" % max(20, h // 12),
        "--osd-color=#FFFF00",          # amarillo
        "--osd-border-color=#000000",
        "--osd-border-size=3",
        "--osd-duration=2000",
    ]


def _player_cmd(name, path, url, audio_url, aspect=0):
    """Linea de comandos de mpv, con la pista de audio separada cuando el
    formato es DASH (audio_url no es None)."""
    extra = CFG.get("video", {}).get(name + "_args", [])
    cmd = [path, "--fs", "--no-terminal", "--really-quiet",
           "--input-ipc-server=" + MPV_SOCKET,
           # Aviso al empezar: sin barra de ayuda durante el video, nadie
           # adivinaria que A pausa o que Y cambia el encaje.
           "--osd-playing-msg=" + T("osd_hint")]
    cmd += _osd_args()
    cmd += ASPECT_MODES[aspect % len(ASPECT_MODES)][2]
    if audio_url:
        cmd.append("--audio-file=" + audio_url)
    return cmd + extra + [url]


NO_PLAYER_TEXT = [
    "No hay reproductor de video y no se pudo descargar.",
    "",
    "Opciones:",
    "  - Reintentar con mejor conexion WiFi.",
    "  - Instalarlo en la consola por SSH:",
    "      sudo apt install mpv",
]


MX, MY = _margin("margin_x"), _margin("margin_y")
# area util (dentro del bisel)
UX, UY, UW, UH = MX, MY, LW - 2 * MX, LH - 2 * MY

THUMB_DIR = os.path.join(BASE, ".thumbs")
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/TTF/DejaVuSans.ttf",
              os.path.join(BASE, "font.ttf")]

C_BG = (24, 24, 24)
C_SIDEBAR = (16, 16, 16)
C_TEXT = (235, 235, 235)
C_DIM = (150, 150, 150)
C_SEL = (230, 40, 40)
C_BADGE = (0, 0, 0)
C_MODAL = (45, 48, 58)

BTN_COLORS = {"A": (200, 40, 40), "B": (220, 180, 30),
              "X": (50, 90, 220), "Y": (40, 180, 70),
              "START": (120, 120, 130)}

# Claves internas de las secciones. Lo que se pinta se traduce con T().
SIDEBAR_ITEMS = ["Home", "Search", "Favorites", "History", "Settings"]

# Idioma actual. En una lista para poder cambiarlo desde Settings sin tener
# que pasar el estado por toda la UI. Por defecto ingles.
LANG = ["en"]

TEXTS = {
    "en": {
        "Home": "Home", "Search": "Search", "Favorites": "Favorites",
        "History": "History", "Settings": "Settings",
        "hb_play": "Play", "hb_quit": "Quit", "hb_search": "Search",
        "hb_fav": "Fav", "hb_refresh": "Refresh", "hb_change": "Change",
        "hb_apply": "Apply", "hb_back": "Back",
        "loading_feed": "Loading feed...", "searching": "Searching...",
        "updating_feed": "Updating feed...",
        "no_results": "No results",
        "no_favorites": "No favorites", "empty_history": "History is empty",
        "no_wifi": "No WiFi - check the network and retry (X)",
        "no_wifi_short": "No WiFi",
        "no_video": "No connection or video unavailable",
        "feed_kept": "Could not update - keeping saved feed",
        "feed_fail": "No connection or no results - Home retries",
        "ytdlp_updated": "yt-dlp updated to %s",
        "no_image": "no image", "loading": "loading", "loading_more": "loading more",
        "error": "Error: %s",
        "exit_q": "Are you sure you want to exit?",
        "yes": "Yes", "no": "No",
        "set_lang": "Language", "set_quality": "Video quality",
        "set_aspect": "Video fit", "set_clear_cache": "Clear cache",
        "set_clear_cookies": "Delete cookies.txt",
        "set_action": "press A",
        "cache_q": "Delete thumbnails and saved feed?",
        "cookies_q": "Delete cookies.txt? The feed stops being personalized.",
        "cache_done": "Cache cleared (%d files)",
        "cookies_done": "cookies.txt deleted",
        "cookies_none": "There was no cookies.txt",
        "panel_max": "max %dp",
        "osd_hint": "A: pause   Y: fit   B: exit",
        "osd_play": "Play", "osd_pause": "Pause",
        "aspect_fit": "Fit", "aspect_fit_hint": "black bars, nothing cropped",
        "aspect_fill": "Fill", "aspect_fill_hint": "no bars, crops the sides",
        "aspect_stretch": "Stretch", "aspect_stretch_hint": "no bars, distorted",
    },
    "es": {
        "Home": "Inicio", "Search": "Buscar", "Favorites": "Favoritos",
        "History": "Historial", "Settings": "Ajustes",
        "hb_play": "Ver", "hb_quit": "Salir", "hb_search": "Buscar",
        "hb_fav": "Favorito", "hb_refresh": "Recargar", "hb_change": "Cambiar",
        "hb_apply": "Aplicar", "hb_back": "Atras",
        "loading_feed": "Cargando feed...", "searching": "Buscando...",
        "updating_feed": "Actualizando feed...",
        "no_results": "Sin resultados",
        "no_favorites": "Sin favoritos", "empty_history": "Historial vacio",
        "no_wifi": "Sin conexion WiFi - revisa la red y reintenta (X)",
        "no_wifi_short": "Sin conexion WiFi",
        "no_video": "Sin conexion o video no disponible",
        "feed_kept": "No se pudo actualizar - se mantiene el feed guardado",
        "feed_fail": "Sin conexion o sin resultados - Home reintenta",
        "ytdlp_updated": "yt-dlp actualizado a %s",
        "no_image": "sin imagen", "loading": "cargando",
        "loading_more": "cargando mas",
        "error": "Error: %s",
        "exit_q": "Seguro que quieres salir?",
        "yes": "Si", "no": "No",
        "set_lang": "Idioma", "set_quality": "Calidad de video",
        "set_aspect": "Encaje del video", "set_clear_cache": "Borrar cache",
        "set_clear_cookies": "Borrar cookies.txt",
        "set_action": "pulsa A",
        "cache_q": "Borrar miniaturas y feed guardado?",
        "cookies_q": "Borrar cookies.txt? El feed dejara de ser personalizado.",
        "cache_done": "Cache borrada (%d ficheros)",
        "cookies_done": "cookies.txt borrado",
        "cookies_none": "No habia cookies.txt",
        "panel_max": "maximo %dp",
        "osd_hint": "A: pausa   Y: encaje   B: salir",
        "osd_play": "Play", "osd_pause": "Pausa",
        "aspect_fit": "Ajustar", "aspect_fit_hint": "barras negras, se ve todo",
        "aspect_fill": "Llenar", "aspect_fill_hint": "sin barras, recorta los lados",
        "aspect_stretch": "Estirar", "aspect_stretch_hint": "sin barras, imagen deformada",
    },
}


def T(key):
    """Texto en el idioma activo, con el ingles como respaldo."""
    return TEXTS.get(LANG[0], TEXTS["en"]).get(key, TEXTS["en"].get(key, key))


DL_TEXTS = {
    "en": {
        "title": "Downloading yt-dlp (video engine)",
        "fail": "Download failed (no WiFi or GitHub down)",
        "manual": [
            "Manual install:",
            "1. Download yt-dlp (the plain python zipapp):",
            "   github.com/yt-dlp/yt-dlp/releases",
            "   (file named just: yt-dlp, about 3 MB)",
            "2. Save it on the console as:",
            "   /roms/ports/youtube_py/yt-dlp.zip",
            "3. Make it executable: chmod +x yt-dlp.zip",
        ],
        "retry": "A: retry   B: continue without playback   Y: Espanol",
    },
    "es": {
        "title": "Descargando yt-dlp (motor de video)",
        "fail": "Fallo la descarga (sin WiFi o GitHub caido)",
        "manual": [
            "Instalacion manual:",
            "1. Descargar yt-dlp (el zipapp de python puro):",
            "   github.com/yt-dlp/yt-dlp/releases",
            "   (el archivo llamado solo: yt-dlp, unos 3 MB)",
            "2. Guardarlo en la consola como:",
            "   /roms/ports/youtube_py/yt-dlp.zip",
            "3. Permisos de ejecucion: chmod +x yt-dlp.zip",
        ],
        "retry": "A: reintentar   B: continuar sin reproduccion   Y: English",
    },
}

COOKIE_TEXTS = {
    "en": {
        "missing": "cookies.txt not found (not signed in)",
        "expired": "Cookies expired (feed not personalized)",
        "steps": [
            "1. On your PC: open an INCOGNITO window",
            "2. Go to youtube.com and sign in",
            "3. Export with the 'Get cookies.txt LOCALLY' extension",
            "4. Close the incognito window WITHOUT signing out",
            "5. Copy the file to the console:",
            "   scp cookies.txt ark@<IP>:/roms/ports/youtube_py/",
            "",
            "Non-personalized content is shown meanwhile.",
        ],
        "footer": "A/B: close   Y: Espanol",
    },
    "es": {
        "missing": "No hay cookies.txt (sesion no iniciada)",
        "expired": "Las cookies caducaron (feed no personalizado)",
        "steps": [
            "1. En el PC: abrir ventana de INCOGNITO",
            "2. Entrar a youtube.com e iniciar sesion",
            "3. Exportar con la extension 'Get cookies.txt LOCALLY'",
            "4. Cerrar el incognito SIN cerrar sesion",
            "5. Copiar el archivo a la consola:",
            "   scp cookies.txt ark@<IP>:/roms/ports/youtube_py/",
            "",
            "Mientras tanto se muestra contenido no personalizado.",
        ],
        "footer": "A/B: cerrar   Y: English",
    },
}


def _r(x, y, w, h):
    return sdl2.SDL_Rect(int(x), int(y), int(w), int(h))


class TextCache(object):
    def __init__(self, ren):
        self.ren = ren
        self.fonts = {}
        self.cache = {}

    def font(self, size):
        if size not in self.fonts:
            for p in FONT_PATHS:
                if os.path.exists(p):
                    self.fonts[size] = ttf.TTF_OpenFont(p.encode(), size)
                    break
        return self.fonts[size]

    def tex(self, text, size, color):
        key = (text, size, color)
        if key in self.cache:
            return self.cache[key]
        if len(self.cache) > 400:
            for t, _, _2 in self.cache.values():
                sdl2.SDL_DestroyTexture(t)
            self.cache.clear()
        col = sdl2.SDL_Color(color[0], color[1], color[2], 255)
        surf = ttf.TTF_RenderUTF8_Blended(self.font(size), text.encode(), col)
        if not surf:
            return None
        tex = sdl2.SDL_CreateTextureFromSurface(self.ren, surf)
        w, h = surf.contents.w, surf.contents.h
        sdl2.SDL_FreeSurface(surf)
        self.cache[key] = (tex, w, h)
        return self.cache[key]

    def draw(self, text, x, y, size=13, color=C_TEXT, max_w=0):
        if not text:
            return 0
        if max_w:
            while text and self.tex(text + "...", size, color)[1] > max_w and len(text) > 4:
                text = text[:-1]
            got = self.tex(text, size, color)
            if got[1] > max_w:
                text += "..."
        t = self.tex(text, size, color)
        if not t:
            return 0
        tex, w, h = t
        dst = _r(x, y, w, h)
        sdl2.SDL_RenderCopy(self.ren, tex, None, dst)
        return h


class ThumbLoader(object):
    """Descarga thumbnails en hilos; el hilo de render las sube a textura."""

    MAX_CACHED = 300   # limite de thumbnails en disco (~3 MB)
    # Limite de texturas VIVAS en RAM. Una miniatura de 320x180 en RGBA ocupa
    # 225 KB, asi que sin tope el scroll las va acumulando: 300 videos vistos
    # serian 66 MB. Con 48 el techo queda en ~11 MB, de sobra para la rejilla
    # visible y el prefetch de alrededor.
    MAX_TEXTURES = 48

    def __init__(self):
        self.ready = {}      # video_id -> ruta de archivo descargado
        self.textures = collections.OrderedDict()   # video_id -> SDL texture
        self.pending = set()
        self.failed = {}     # video_id -> tick del fallo (para reintentar)
        self.lock = threading.Lock()
        os.makedirs(THUMB_DIR, exist_ok=True)
        threading.Thread(target=self._prune, daemon=True).start()

    def _prune(self):
        """Mantener solo los MAX_CACHED thumbnails mas recientes."""
        try:
            files = [os.path.join(THUMB_DIR, f) for f in os.listdir(THUMB_DIR)]
            files.sort(key=os.path.getmtime, reverse=True)
            for f in files[self.MAX_CACHED:]:
                os.remove(f)
        except OSError:
            pass

    def request(self, video):
        vid = video.id
        now = sdl2.SDL_GetTicks()
        with self.lock:
            if vid in self.textures or vid in self.ready or vid in self.pending:
                return
            # tras un fallo (wifi caido), reintentar a los 30s
            if vid in self.failed and now - self.failed[vid] < 30000:
                return
            self.failed.pop(vid, None)
            self.pending.add(vid)
        t = threading.Thread(target=self._fetch, args=(video,), daemon=True)
        t.start()

    SEM = threading.Semaphore(8)   # max descargas simultaneas

    def _fetch(self, video):
        with self.SEM:
            dest = os.path.join(THUMB_DIR, video.id + ".jpg")
            ok = os.path.exists(dest) or backend.fetch_thumbnail(video.thumbnail, dest)
        with self.lock:
            if ok:
                self.ready[video.id] = dest
            else:
                self.failed[video.id] = sdl2.SDL_GetTicks()
            self.pending.discard(video.id)

    def texture(self, ren, video):
        """Textura de la miniatura. Se llama SIEMPRE desde el hilo de render,
        que es el unico que puede crear y destruir texturas SDL."""
        vid = video.id
        tex = self.textures.get(vid)
        if tex is not None:
            self.textures.move_to_end(vid)      # LRU: recien usada
            return tex
        with self.lock:
            path = self.ready.pop(vid, None)
        if path:
            tex = img.IMG_LoadTexture(ren, path.encode())
            if tex:
                self.textures[vid] = tex
                self._evict()
                return tex
        return None

    def _evict(self):
        """Destruye las texturas mas antiguas por encima del tope."""
        while len(self.textures) > self.MAX_TEXTURES:
            _, old = self.textures.popitem(last=False)
            sdl2.SDL_DestroyTexture(old)

    def forget_textures(self):
        """Olvida las texturas SIN destruirlas.

        Se usa tras recrear el renderer: SDL_DestroyRenderer ya destruyo sus
        texturas, asi que aqui solo quedan punteros colgando y llamar a
        SDL_DestroyTexture seria un doble free."""
        self.textures.clear()


class App(object):
    def __init__(self):
        # El idioma se lee antes de tocar la UI: todo lo que se pinta pasa
        # por T(). Ingles por defecto.
        LANG[0] = CFG.get("lang", "en")
        if LANG[0] not in TEXTS:
            LANG[0] = "en"
        sdl2.SDL_SetHint(b"SDL_RENDER_SCALE_QUALITY", b"1")
        sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO | sdl2.SDL_INIT_GAMECONTROLLER)
        ttf.TTF_Init()
        img.IMG_Init(img.IMG_INIT_JPG | img.IMG_INIT_PNG)
        self._open_display()
        self.text = TextCache(self.ren)
        self.thumbs = ThumbLoader()
        self.pad = None
        self._open_pad()

        self.sidebar_idx = 0
        self.in_sidebar = True     # foco inicial en Home (es lo que carga)
        self.settings_idx = 0      # opcion elegida en Ajustes
        self.grid_idx = 0
        self.scroll_row = 0
        self.videos = []
        self.status = T("loading_feed")
        self.cookie_warn = False
        self.cookie_popup = None   # 'missing' | 'expired' -> muestra ventana
        self.cookie_lang = LANG[0]
        self.modal = None          # (pregunta, [opciones], idx, callback)
        self.running = True
        self.section = "Home"
        self.search_token = None      # continuacion de busqueda innertube
        self.feed_offset = 0          # paginacion del home
        self.loading_more = False
        self.refreshing = False       # START: refresco del feed en curso
        self.aspect = self._load_aspect()   # modo de encaje del video
        self.favorites = self._load_json("favorites.json")
        self.history = self._load_json("history.json")

        if not backend.ytdlp_present():
            self._download_ytdlp()
        else:
            # en background: actualizar yt-dlp si hay version nueva
            def upd():
                v = backend.update_ytdlp_if_needed()
                if v:
                    self.status = T("ytdlp_updated") % v
            threading.Thread(target=upd, daemon=True).start()

        # Precalentar el motor de yt-dlp mientras el usuario mira el feed.
        # Importarlo cuesta ~6 s en esta CPU; hacerlo aqui, en segundo plano,
        # significa que la primera reproduccion ya no los paga.
        threading.Thread(
            target=backend.warmup_ytdlp, args=(CFG["quality"],),
            daemon=True).start()

        # Reproductor: se comprueba al arrancar, no al pulsar play, para que
        # el usuario se entere antes de elegir un video.
        if _which_player()[0] is None:
            self._install_mpv()

        # El feed arranca desde el cache: instantaneo y sin red. Para traer
        # novedades esta START.
        threading.Thread(target=self._load_home, kwargs={"refresh": False},
                         daemon=True).start()

    # ---------- descarga automatica de yt-dlp ----------
    def _download_ytdlp(self):
        """Pantalla bloqueante: descarga yt-dlp con progreso; si falla,
        instrucciones manuales con reintento."""
        lang = ["en"]
        while self.running and not backend.ytdlp_present():
            progress = [0]
            error = [False]
            done = [False]

            def worker():
                try:
                    backend.download_ytdlp(
                        lambda p: progress.__setitem__(0, p))
                except Exception:
                    error[0] = True
                done[0] = True
            threading.Thread(target=worker, daemon=True).start()

            ev = sdl2.SDL_Event()
            frame = 0
            while not done[0] and self.running:
                while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                    if ev.type == sdl2.SDL_QUIT:
                        self.running = False
                t = DL_TEXTS[lang[0]]
                self._draw_frame()
                self._draw_loading("%s %d%%" % (t["title"], progress[0]),
                                   frame)
                sdl2.SDL_RenderPresent(self.ren)
                frame += 1
                sdl2.SDL_Delay(120)

            if not error[0]:
                return   # descargado

            # fallo: instrucciones manuales + reintentar/continuar
            choice = [None]
            while choice[0] is None and self.running:
                while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                    if ev.type == sdl2.SDL_QUIT:
                        self.running = False
                    elif ev.type == sdl2.SDL_CONTROLLERBUTTONDOWN:
                        b = self.BTN.get(ev.cbutton.button)
                        if b == "A":
                            choice[0] = "retry"
                        elif b == "B":
                            choice[0] = "skip"
                        elif b == "Y":
                            lang[0] = "es" if lang[0] == "en" else "en"
                t = DL_TEXTS[lang[0]]
                self._draw_frame()
                mw, mh = 470, 260
                mx0 = UX + (UW - mw) // 2
                my0 = UY + (UH - mh) // 2
                self._fill(UX, UY, UW, UH, (0, 0, 0), 150)
                self._fill_round(mx0, my0, mw, mh, C_MODAL, 250, rad=12)
                self.text.draw(t["fail"], mx0 + 20, my0 + 14, 14,
                               (230, 200, 60))
                y = my0 + 46
                for line in t["manual"]:
                    self.text.draw(line, mx0 + 20, y, 12)
                    y += 20
                self.text.draw(t["retry"], mx0 + 20, my0 + mh - 26, 11, C_DIM)
                sdl2.SDL_RenderPresent(self.ren)
                sdl2.SDL_Delay(50)
            if choice[0] == "skip":
                return

    # ---------- display ----------
    def _open_display(self):
        self.win = sdl2.SDL_CreateWindow(b"YouTube", 0, 0, LW, LH,
                                         sdl2.SDL_WINDOW_FULLSCREEN)
        self.ren = sdl2.SDL_CreateRenderer(self.win, -1,
                                           sdl2.SDL_RENDERER_ACCELERATED)
        if not self.ren:
            self.ren = sdl2.SDL_CreateRenderer(self.win, -1,
                                               sdl2.SDL_RENDERER_SOFTWARE)
        sdl2.SDL_RenderSetLogicalSize(self.ren, LW, LH)

    def _close_display(self):
        sdl2.SDL_DestroyRenderer(self.ren)
        sdl2.SDL_DestroyWindow(self.win)
        sdl2.SDL_QuitSubSystem(sdl2.SDL_INIT_VIDEO)

    def _reopen_display(self):
        sdl2.SDL_InitSubSystem(sdl2.SDL_INIT_VIDEO)
        self._open_display()
        self.text = TextCache(self.ren)
        self.thumbs.forget_textures()

    def _open_pad(self):
        for i in range(sdl2.SDL_NumJoysticks()):
            if sdl2.SDL_IsGameController(i):
                self.pad = sdl2.SDL_GameControllerOpen(i)
                return
        if sdl2.SDL_NumJoysticks() > 0:
            sdl2.SDL_JoystickOpen(0)

    # ---------- data ----------
    def _load_json(self, name):
        try:
            return [backend.Video(**v) for v in
                    json.load(open(os.path.join(BASE, name)))]
        except Exception:
            return []

    def _save_json(self, name, vids):
        data = [{"id": v.id, "title": v.title, "channel": v.channel,
                 "thumbnail": v.thumbnail, "duration": v.duration,
                 "view_count": v.view_count} for v in vids if not v.is_notice]
        with open(os.path.join(BASE, name), "w") as f:
            json.dump(data, f)

    def _load_home(self, refresh=True):
        """Carga el feed de inicio.

        Con refresh=False se queda con el cache si existe y NO toca la red:
        el feed remoto tarda ~45 s en esta consola y sustituir la lista a
        medias hacia perder la posicion y recargar todas las miniaturas.
        El refresco va aparte, con START."""
        cached = self._load_json("feed_cache.json")
        if cached:
            self.videos = cached
            self.status = ""
            self._prefetch(cached)
            if not refresh:
                self._fill_channels(cached)
                return
        # refrescar contra la red
        vids, cookie_state = backend.home_feed(CFG["search_count"] * 2)
        if vids:
            self.videos = vids
            self.status = ""
            self.grid_idx = 0
            self.scroll_row = 0
            self.feed_offset = 0
            self._save_json("feed_cache.json", vids)
            self._prefetch(vids)
        elif not cached:
            self.status = T("feed_fail")
        else:
            self.status = T("feed_kept")
        if cookie_state != "ok":
            self.cookie_popup = cookie_state
        self._fill_channels(self.videos)

    def _refresh_home(self):
        """START: recarga el feed contra la red.

        Bloquea con un cartel mientras dura: son ~10 s en los que la lista
        vieja sigue en pantalla, y sin aviso parece que el boton no hizo
        nada (o peor, invita a pulsarlo otra vez)."""
        if self.section != "Home" or self.refreshing:
            return
        self.refreshing = True
        self.status = T("updating_feed")
        done = [False]

        def worker():
            try:
                self._load_home(refresh=True)
            except backend.NetworkError:
                self.status = T("no_wifi_short")
            except Exception as e:               # noqa: BLE001
                self.status = T("error") % e
            finally:
                self.refreshing = False
                done[0] = True
        threading.Thread(target=worker, daemon=True).start()

        ev = sdl2.SDL_Event()
        frame = 0
        while not done[0] and self.running:
            while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                if ev.type == sdl2.SDL_QUIT:
                    self.running = False
            self._draw_frame()
            self._fill(UX, UY, UW, UH, (0, 0, 0), 120)
            self._draw_loading(T("updating_feed").rstrip("."), frame)
            sdl2.SDL_RenderPresent(self.ren)
            frame += 1
            sdl2.SDL_Delay(100)

    def _prefetch(self, vids):
        # solo las 2 filas visibles (4 thumbnails); el resto se pide al hacer scroll
        for v in vids[:4]:
            if not v.is_notice:
                self.thumbs.request(v)

    def _fill_channels(self, vids):
        """El feed plano de yt-dlp no trae canal; completarlo via Innertube."""
        def worker():
            for v in vids:
                if not self.running or self.videos is not vids:
                    return
                if not v.channel:
                    v.channel = backend.channel_of(v.id)
        threading.Thread(target=worker, daemon=True).start()

    def _load_more(self):
        """Scroll infinito: cargar la siguiente tanda al acercarse al final."""
        if self.loading_more:
            return
        if self.section == "Home":
            self.loading_more = True
            vids = self.videos

            def worker():
                try:
                    self.feed_offset += CFG["search_count"] * 2
                    more, _st = backend.home_feed(CFG["search_count"] * 2,
                                                  self.feed_offset)
                    seen = set(v.id for v in vids)
                    more = [v for v in more if v.id not in seen]
                    if more and self.videos is vids:
                        self.videos = vids + more
                        self._fill_channels(more)
                except backend.NetworkError:
                    self.status = T("no_wifi_short")
                finally:
                    self.loading_more = False
            threading.Thread(target=worker, daemon=True).start()
        elif self.section == "Search" and self.search_token:
            self.loading_more = True
            vids = self.videos
            token = self.search_token

            def worker2():
                try:
                    more, nxt = backend.search_continue(token,
                                                        CFG["search_count"] * 2)
                    if self.videos is vids:
                        self.videos = vids + more
                        self.search_token = nxt
                except backend.NetworkError:
                    self.status = T("no_wifi_short")
                finally:
                    self.loading_more = False
            threading.Thread(target=worker2, daemon=True).start()

    def _do_search(self, query):
        self.status = T("searching")
        self.videos = []
        self.grid_idx = 0
        self.scroll_row = 0

        def worker():
            try:
                self.videos, self.search_token = backend.search(
                    query, CFG["search_count"] * 2)
                self.status = "" if self.videos else T("no_results")
                self._prefetch(self.videos)
            except backend.NetworkError:
                self.status = T("no_wifi")
            except Exception as e:
                self.status = T("error") % e
        threading.Thread(target=worker, daemon=True).start()

    # ---------- playback ----------
    def _install_mpv(self):
        """Descarga mpv (~4 MB) tras PEDIR CONFIRMACION al usuario.

        No instala nada en el sistema ni toca apt: el binario y sus librerias
        quedan en youtube_py/{bin,lib} y solo los usa el reproductor.
        Devuelve True si mpv quedo disponible."""
        ev = sdl2.SDL_Event()
        choice = [None]
        info = [
            "Falta mpv, el reproductor de video.",
            "",
            "Puedo descargarlo (unos 4 MB) dentro de la",
            "carpeta del port: bin/mpv y lib/.",
            "",
            "No se instala nada en el sistema ni se usa apt.",
            "Para quitarlo basta con borrar esas carpetas.",
        ]
        footer = "A = descargar    B = cancelar"
        while choice[0] is None and self.running:
            while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                if ev.type == sdl2.SDL_QUIT:
                    self.running = False
                elif ev.type == sdl2.SDL_CONTROLLERBUTTONDOWN:
                    b = self.BTN.get(ev.cbutton.button)
                    if b == "A":
                        choice[0] = "yes"
                    elif b == "B":
                        choice[0] = "no"
            self._modal_box("Descargar reproductor de video", info, footer)
            sdl2.SDL_RenderPresent(self.ren)
            sdl2.SDL_Delay(50)
        if choice[0] != "yes":
            return False

        progress = [0]
        error = [None]

        def worker():
            try:
                backend.download_mpv(lambda p: progress.__setitem__(0, p))
            except backend.NetworkError as e:
                error[0] = str(e)
            except Exception as e:            # noqa: BLE001
                error[0] = str(e)
        th = threading.Thread(target=worker, daemon=True)
        th.start()
        while th.is_alive() and self.running:
            while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                if ev.type == sdl2.SDL_QUIT:
                    self.running = False
            self._draw_frame()
            self._draw_loading("Descargando mpv %d%%" % progress[0], 0)
            sdl2.SDL_RenderPresent(self.ren)
            sdl2.SDL_Delay(120)
        if error[0]:
            self._error_modal("No se pudo descargar mpv",
                              ["Error: " + error[0][:44], "",
                               "Revisa la conexion WiFi y vuelve a intentarlo."])
            return False
        return True

    def play(self, video):
        if video.is_notice:
            return
        # resolver stream en hilo, con spinner animado en pantalla
        result = {}

        def worker():
            result["url"], result["audio"] = backend.resolve_stream(
                video, CFG["quality"])
        th = threading.Thread(target=worker, daemon=True)
        th.start()
        frame = 0
        ev = sdl2.SDL_Event()
        while th.is_alive() and self.running:
            while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                pass
            self._draw_frame()
            self._draw_loading("Cargando video", frame)
            sdl2.SDL_RenderPresent(self.ren)
            frame += 1
            sdl2.SDL_Delay(80)
        url = result.get("url")
        if not url:
            self.status = T("no_video")
            return
        name, path = _which_player()
        if not name:
            if not self._install_mpv():
                return
            name, path = _which_player()
            if not name:
                self._error_modal("Falta un reproductor de video",
                                  NO_PLAYER_TEXT)
                self.running = False
                return
        self.history = [video] + [v for v in self.history if v.id != video.id]
        self._save_json("history.json", self.history[:100])
        self._close_display()
        try:
            try:
                os.remove(MPV_SOCKET)      # restos de una sesion anterior
            except OSError:
                pass
            cmdline = _player_cmd(name, path, url, result.get("audio"),
                                  self.aspect)
            _vlog("lanzando: %s" % " ".join(
                a for a in cmdline if a.startswith("--") or a == path))
            proc = subprocess.Popen(cmdline, env=_player_env())
            # Durante el video:  B = salir   A = pausa/continuar
            #                    Y = rotar el modo de encaje
            # Se ignora el estado inicial de cada boton para que el mismo
            # pulsado con el que se entro al video no cuente como evento.
            was = {"A": True, "B": True, "Y": True}
            btns = {"A": sdl2.SDL_CONTROLLER_BUTTON_A,
                    "B": sdl2.SDL_CONTROLLER_BUTTON_B,
                    "Y": sdl2.SDL_CONTROLLER_BUTTON_Y}
            while proc.poll() is None:
                sdl2.SDL_GameControllerUpdate()
                now = dict((k, bool(self.pad and
                                    sdl2.SDL_GameControllerGetButton(self.pad, v)))
                           for k, v in btns.items())
                if now["B"] and not was["B"]:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                if now["A"] and not was["A"]:
                    _vlog("A -> pausa/continuar")
                    self._toggle_pause()
                if now["Y"] and not was["Y"]:
                    _vlog("Y -> cambio de encaje")
                    self._cycle_aspect()
                was = now
                sdl2.SDL_Delay(50)
        finally:
            # esperar a que se suelte B para que no llegue a la UI
            while self.pad:
                sdl2.SDL_GameControllerUpdate()
                if not sdl2.SDL_GameControllerGetButton(
                        self.pad, sdl2.SDL_CONTROLLER_BUTTON_B):
                    break
                sdl2.SDL_Delay(30)
            self._reopen_display()
            # descartar eventos acumulados durante la reproduccion
            ev = sdl2.SDL_Event()
            while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                pass

    def _toggle_pause(self):
        """A durante el video: pausa o continua."""
        paused = _mpv_query(["pause"]).get("pause")
        ok = _mpv_ipc(_osd_setup_ipc() +
                      [["cycle", "pause"],
                       ["show-text",
                        T("osd_play") if paused else T("osd_pause"), 900]])
        _vlog("  pausa: estado previo=%r  ipc_ok=%s  ahora=%r"
              % (paused, ok, _mpv_query(["pause"]).get("pause")))

    def _cycle_aspect(self):
        """Y durante el video: pasa al siguiente modo de encaje.

        Se aplica en caliente por el socket IPC de mpv (sin reiniciar la
        reproduccion), avisa en pantalla y se recuerda para los siguientes
        videos."""
        self.aspect = (self.aspect + 1) % len(ASPECT_MODES)
        name_key, hint_key, _ = ASPECT_MODES[self.aspect]
        name, hint = T(name_key), T(hint_key)
        vp = _mpv_query(["width", "height"])
        ratio = _ratio_text(vp.get("width"), vp.get("height"))
        text = "%d/%d  %s\n%s" % (self.aspect + 1, len(ASPECT_MODES),
                                  name, hint)
        if ratio:
            text += "\n" + ratio
        cmds = _osd_setup_ipc() + list(ASPECT_IPC[self.aspect])
        cmds.append(["show-text", text, ASPECT_OSD_MS])
        ok = _mpv_ipc(cmds)
        _vlog("  encaje -> %s  ipc_ok=%s  panscan=%r keepaspect=%r"
              % (name, ok, _mpv_query(["panscan"]).get("panscan"),
                 _mpv_query(["keepaspect"]).get("keepaspect")))
        if ok:
            self._save_aspect()

    def _save_aspect(self):
        try:
            with open(os.path.join(BASE, "aspect.json"), "w") as f:
                json.dump({"aspect": self.aspect}, f)
        except OSError:
            pass

    def _load_aspect(self):
        try:
            with open(os.path.join(BASE, "aspect.json")) as f:
                v = int(json.load(f).get("aspect", 0))
                return v % len(ASPECT_MODES)
        except (OSError, ValueError, TypeError):
            return 0

    # ---------- input ----------
    BTN = {sdl2.SDL_CONTROLLER_BUTTON_A: "A", sdl2.SDL_CONTROLLER_BUTTON_B: "B",
           sdl2.SDL_CONTROLLER_BUTTON_X: "X", sdl2.SDL_CONTROLLER_BUTTON_Y: "Y",
           sdl2.SDL_CONTROLLER_BUTTON_DPAD_UP: "UP",
           sdl2.SDL_CONTROLLER_BUTTON_DPAD_DOWN: "DOWN",
           sdl2.SDL_CONTROLLER_BUTTON_DPAD_LEFT: "LEFT",
           sdl2.SDL_CONTROLLER_BUTTON_DPAD_RIGHT: "RIGHT",
           sdl2.SDL_CONTROLLER_BUTTON_START: "START"}

    def handle(self, btn):
        if self.cookie_popup:
            if btn in ("A", "B"):
                self.cookie_popup = None
            elif btn == "Y":
                self.cookie_lang = "es" if self.cookie_lang == "en" else "en"
            return
        if self.modal:
            self._handle_modal(btn)
            return
        if btn == "B":            # B = atras/salir
            # En Ajustes, B vuelve a la barra lateral en vez de salir de la
            # app: pedir confirmacion de salida ahi seria desconcertante.
            if self.section == "Settings" and not self.in_sidebar:
                self.in_sidebar = True
            else:
                self._confirm_exit()
        elif btn == "A":          # A = seleccionar/reproducir
            if self.in_sidebar:
                self._enter_section()
            elif self.section == "Settings":
                self._settings_activate()
            elif self.videos:
                self.play(self.videos[self.grid_idx])
        elif btn == "Y":
            if self.section != "Settings":
                self._toggle_fav()
        elif btn == "X":
            self._enter_search()
        elif btn == "START":
            self._refresh_home()
        elif btn in ("UP", "DOWN", "LEFT", "RIGHT"):
            self._navigate(btn)

    def _navigate(self, btn):
        if self.in_sidebar:
            if btn == "UP":
                self.sidebar_idx = max(0, self.sidebar_idx - 1)
            elif btn == "DOWN":
                self.sidebar_idx = min(len(SIDEBAR_ITEMS) - 1, self.sidebar_idx + 1)
            elif btn == "RIGHT":
                self.in_sidebar = False
            return
        if self.section == "Settings":
            n = len(self._settings_items())
            if btn == "UP":
                self.settings_idx = (self.settings_idx - 1) % n
            elif btn == "DOWN":
                self.settings_idx = (self.settings_idx + 1) % n
            elif btn == "LEFT":
                self._settings_change(-1)
            elif btn == "RIGHT":
                self._settings_change(1)
            return
        n = len(self.videos)
        if not n:
            if btn == "LEFT":
                self.in_sidebar = True
            return
        i = self.grid_idx
        if btn == "LEFT":
            if i % 2 == 0:
                self.in_sidebar = True
            else:
                i -= 1
        elif btn == "RIGHT" and i % 2 == 0 and i + 1 < n:
            i += 1
        elif btn == "UP" and i >= 2:
            i -= 2
        elif btn == "DOWN":
            if i + 2 < n:
                i += 2
            elif i < n - 1:
                i = n - 1
            if i + 4 >= n:
                self._load_more()
        self.grid_idx = i
        row = i // 2
        if row < self.scroll_row:
            self.scroll_row = row
        elif row > self.scroll_row + 1:
            self.scroll_row = row - 1

    # ---------- ajustes ----------
    def _quality_options(self):
        """Resoluciones ofrecidas, topadas por la del panel.

        No tiene sentido pedir 720p en una pantalla de 480: solo gastaria CPU
        decodificando pixeles que hay que tirar al escalar."""
        _, panel_h = _screen_size()
        return [q for q in (144, 240, 360, 480, 720) if q <= panel_h] or [360]

    def _settings_items(self):
        """Lista de (clave, etiqueta, valor_mostrado). Se recalcula al vuelo
        para que refleje el idioma y los valores actuales."""
        _, panel_h = _screen_size()
        langs = {"en": "English", "es": "Espanol"}
        return [
            ("lang", T("set_lang"), langs.get(LANG[0], LANG[0])),
            ("quality", T("set_quality"),
             "%dp   %s" % (CFG["quality"], T("panel_max") % panel_h)),
            ("aspect", T("set_aspect"), T(ASPECT_MODES[self.aspect][0])),
            ("cache", T("set_clear_cache"), T("set_action")),
            ("cookies", T("set_clear_cookies"), T("set_action")),
        ]

    def _settings_change(self, delta):
        """LEFT/RIGHT sobre una opcion con valores."""
        key = self._settings_items()[self.settings_idx][0]
        if key == "lang":
            order = ["en", "es"]
            LANG[0] = order[(order.index(LANG[0]) + delta) % len(order)]
            self.cookie_lang = LANG[0]
            self._save_config()
        elif key == "quality":
            opts = self._quality_options()
            i = opts.index(CFG["quality"]) if CFG["quality"] in opts else 0
            CFG["quality"] = opts[(i + delta) % len(opts)]
            self._save_config()
        elif key == "aspect":
            self.aspect = (self.aspect + delta) % len(ASPECT_MODES)
            self._save_aspect()

    def _settings_activate(self):
        """A sobre una opcion: acciones destructivas piden confirmacion."""
        key = self._settings_items()[self.settings_idx][0]
        if key == "cache":
            self.modal = [T("cache_q"), [T("no"), T("yes")], 0,
                          lambda ans: self._clear_cache()
                          if ans == T("yes") else None]
        elif key == "cookies":
            self.modal = [T("cookies_q"), [T("no"), T("yes")], 0,
                          lambda ans: self._clear_cookies()
                          if ans == T("yes") else None]
        else:
            self._settings_change(1)

    def _clear_cache(self):
        """Borra miniaturas y feed guardado. No toca favoritos ni historial."""
        n = 0
        try:
            for f in os.listdir(THUMB_DIR):
                try:
                    os.remove(os.path.join(THUMB_DIR, f))
                    n += 1
                except OSError:
                    pass
        except OSError:
            pass
        for name in ("feed_cache.json",):
            try:
                os.remove(os.path.join(BASE, name))
                n += 1
            except OSError:
                pass
        self.thumbs.forget_textures()
        self.thumbs.ready.clear()
        self.thumbs.failed.clear()
        self.status = T("cache_done") % n

    def _clear_cookies(self):
        try:
            os.remove(backend.COOKIES)
            self.status = T("cookies_done")
        except OSError:
            self.status = T("cookies_none")

    def _save_config(self):
        """Guarda idioma y calidad en config.json, conservando el resto."""
        CFG["lang"] = LANG[0]
        try:
            with open(os.path.join(BASE, "config.json"), "w") as f:
                json.dump(CFG, f, indent=2)
        except OSError:
            pass

    def _enter_section(self):
        self.section = SIDEBAR_ITEMS[self.sidebar_idx]
        self.in_sidebar = False
        self.grid_idx = 0
        self.scroll_row = 0
        if self.section == "Settings":
            self.settings_idx = 0
            self.status = ""
        elif self.section == "Home":
            # Volver a Home muestra el cache al instante; para traer
            # novedades esta START.
            self.status = T("loading_feed")
            self.feed_offset = 0
            threading.Thread(target=self._load_home,
                             kwargs={"refresh": False}, daemon=True).start()
        elif self.section == "Favorites":
            self.videos = list(self.favorites)
            self.status = "" if self.videos else T("no_favorites")
        elif self.section == "History":
            self.videos = list(self.history)
            self.status = "" if self.videos else T("empty_history")
        elif self.section == "Search":
            self._enter_search()

    def _toggle_fav(self):
        if not self.videos or self.in_sidebar:
            return
        v = self.videos[self.grid_idx]
        if v.is_notice:
            return
        if any(f.id == v.id for f in self.favorites):
            self.favorites = [f for f in self.favorites if f.id != v.id]
            self.status = "Quitado de favoritos"
        else:
            self.favorites.append(v)
            self.status = "Anadido a favoritos"
        self._save_json("favorites.json", self.favorites)

    def _confirm_exit(self):
        self.modal = [T("exit_q"), [T("no"), T("yes")], 0,
                      lambda ans: setattr(self, "running", ans != T("yes"))]

    def _handle_modal(self, btn):
        q, opts, idx, cb = self.modal
        if btn == "LEFT":
            self.modal[2] = max(0, idx - 1)
        elif btn == "RIGHT":
            self.modal[2] = min(len(opts) - 1, idx + 1)
        elif btn == "A":
            self.modal = None
            cb(opts[idx])
        elif btn == "B":
            self.modal = None

    # ---------- busqueda con teclado ----------
    KB_ROWS = ["abcdefghij", "klmnopqrst", "uvwxyz0123", "456789 -._"]

    def _enter_search(self):
        query = [""]
        kx, ky = [0], [0]
        active = [True]

        def draw():
            self._draw_frame()
            # panel
            pw, ph = 400, 260
            px, py = UX + (UW - pw) // 2, UY + (UH - ph) // 2
            self._fill_round(px, py, pw, ph, C_MODAL, 240, rad=12)
            self.text.draw("Buscar:", px + 16, py + 12, 14)
            self._fill_round(px + 16, py + 34, pw - 32, 24, (20, 20, 20), rad=6)
            self.text.draw(query[0] + "_", px + 22, py + 38, 14)
            for r, row in enumerate(self.KB_ROWS):
                for c, ch in enumerate(row):
                    cx = px + 16 + c * 37
                    cy = py + 74 + r * 38
                    sel = (kx[0] == c and ky[0] == r)
                    self._fill_round(cx, cy, 33, 32, C_SEL if sel else (60, 60, 60), rad=7)
                    self.text.draw(ch if ch != " " else "sp", cx + 9, cy + 7, 14)
            self.text.draw("A:Escribir  B:Borrar  START:Buscar  Y:Cancelar",
                           px + 16, py + ph - 24, 11, C_DIM)
            sdl2.SDL_RenderPresent(self.ren)

        while active[0] and self.running:
            ev = sdl2.SDL_Event()
            while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                if ev.type == sdl2.SDL_CONTROLLERBUTTONDOWN:
                    b = self.BTN.get(ev.cbutton.button)
                    if b == "LEFT":
                        kx[0] = max(0, kx[0] - 1)
                    elif b == "RIGHT":
                        kx[0] = min(9, kx[0] + 1)
                    elif b == "UP":
                        ky[0] = max(0, ky[0] - 1)
                    elif b == "DOWN":
                        ky[0] = min(3, ky[0] + 1)
                    elif b == "A":
                        query[0] += self.KB_ROWS[ky[0]][kx[0]]
                    elif b == "B":
                        query[0] = query[0][:-1]
                    elif b == "Y":
                        active[0] = False
                    elif b == "START":
                        active[0] = False
                        if query[0].strip():
                            self.section = "Search"
                            self._do_search(query[0].strip())
                elif ev.type == sdl2.SDL_QUIT:
                    self.running = False
            draw()
            sdl2.SDL_Delay(30)

    # ---------- render ----------
    def _fill(self, x, y, w, h, color, alpha=255):
        sdl2.SDL_SetRenderDrawBlendMode(self.ren, sdl2.SDL_BLENDMODE_BLEND)
        sdl2.SDL_SetRenderDrawColor(self.ren, color[0], color[1], color[2], alpha)
        sdl2.SDL_RenderFillRect(self.ren, _r(x, y, w, h))

    def _fill_round(self, x, y, w, h, color, alpha=255, rad=8):
        """Rectangulo con esquinas redondeadas (SDL2_gfx)."""
        if gfx:
            gfx.roundedBoxRGBA(self.ren, int(x), int(y), int(x + w - 1),
                               int(y + h - 1), rad,
                               color[0], color[1], color[2], alpha)
        else:
            self._fill(x, y, w, h, color, alpha)

    def _rect_border(self, x, y, w, h, color, thick=2):
        sdl2.SDL_SetRenderDrawColor(self.ren, color[0], color[1], color[2], 255)
        for i in range(thick):
            sdl2.SDL_RenderDrawRect(self.ren, _r(x - i, y - i, w + 2 * i, h + 2 * i))

    def _fmt_dur(self, sec):
        if sec >= 3600:
            return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)
        return "%d:%02d" % (sec // 60, sec % 60)

    SPIN = ["|", "/", "-", "\\"]

    def _draw_loading(self, label, frame):
        """Caja centrada con spinner animado."""
        mw, mh = 280, 56
        x = UX + (UW - mw) // 2
        y = UY + (UH - mh) // 2
        self._fill(UX, UY, UW, UH, (0, 0, 0), 120)
        self._fill_round(x, y, mw, mh, C_MODAL, 250, rad=10)
        spin = self.SPIN[frame % 4]
        dots = "." * (1 + frame % 3)
        self.text.draw(spin, x + 18, y + 18, 14, C_SEL)
        self.text.draw(label + dots, x + 38, y + 18, 14)

    def _modal_box(self, title, lines, footer):
        """Dibuja (sin bloquear) una caja modal con titulo, texto y pie."""
        mw, mh = 470, 220
        mx0 = UX + (UW - mw) // 2
        my0 = UY + (UH - mh) // 2
        self._draw_frame()
        self._fill(UX, UY, UW, UH, (0, 0, 0), 150)
        self._fill_round(mx0, my0, mw, mh, C_MODAL, 250, rad=12)
        self.text.draw(title, mx0 + 20, my0 + 14, 14, (230, 200, 60))
        y = my0 + 46
        for line in lines:
            self.text.draw(line, mx0 + 20, y, 12)
            y += 20
        self.text.draw(footer, mx0 + 20, my0 + mh - 26, 11, C_DIM)

    def _error_modal(self, title, lines, footer="A / B = salir"):
        """Ventana de error bloqueante. Devuelve al pulsar A o B."""
        ev = sdl2.SDL_Event()
        done = False
        while not done and self.running:
            while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                if ev.type == sdl2.SDL_QUIT:
                    self.running = False
                elif ev.type == sdl2.SDL_CONTROLLERBUTTONDOWN:
                    if self.BTN.get(ev.cbutton.button) in ("A", "B"):
                        done = True
                elif ev.type == sdl2.SDL_KEYDOWN:
                    done = True
            self._modal_box(title, lines, footer)
            sdl2.SDL_RenderPresent(self.ren)
            sdl2.SDL_Delay(50)

    def _draw_frame(self):
        sdl2.SDL_SetRenderDrawColor(self.ren, *C_BG, 255)
        sdl2.SDL_RenderClear(self.ren)
        sb_w = 120
        # sidebar
        self._fill(UX, UY, sb_w, UH, C_SIDEBAR)
        self.text.draw("YouTube", UX + 12, UY + 8, 16, (230, 40, 40))
        for i, item in enumerate(SIDEBAR_ITEMS):
            y = UY + 46 + i * 34
            sel = (i == self.sidebar_idx)
            if sel and self.in_sidebar:
                self._fill_round(UX + 4, y - 4, sb_w - 8, 26, (50, 50, 50), rad=6)
            self.text.draw(T(item), UX + 14, y, 13,
                           C_TEXT if sel else C_DIM)
        # cabecera
        gx = UX + sb_w + 12
        gw = UW - sb_w - 12
        self.text.draw(T(self.section), gx, UY, 20)
        if self.section == "Settings":
            self._draw_settings(gx, gw)
            self._draw_help_bar()
            self._draw_overlays()
            return
        # grid 2 columnas x 2 filas visibles
        cw = (gw - 12) // 2
        ch = (UH - 66) // 2
        th = ch - 34   # alto del thumbnail
        first = self.scroll_row * 2
        for k in range(4):
            idx = first + k
            if idx >= len(self.videos):
                break
            v = self.videos[idx]
            col, row = k % 2, k // 2
            x = gx + col * (cw + 12)
            y = UY + 32 + row * (ch + 6)
            tex = None if v.is_notice else self.thumbs.texture(self.ren, v)
            if tex:
                sdl2.SDL_RenderCopy(self.ren, tex, None, _r(x, y, cw, th))
            else:
                self._fill(x, y, cw, th, (40, 40, 40))
                if not v.is_notice:
                    self.thumbs.request(v)
                    if v.id in self.thumbs.failed:
                        self.text.draw(T("no_image"), x + cw // 2 - 30,
                                       y + th // 2 - 8, 11, C_DIM)
                    else:
                        dots = "." * (1 + sdl2.SDL_GetTicks() // 400 % 3)
                        self.text.draw(T("loading") + dots, x + cw // 2 - 30,
                                       y + th // 2 - 8, 11, C_DIM)
            if v.duration:
                dtxt = self._fmt_dur(v.duration)
                self._fill_round(x + cw - 46, y + th - 18, 44, 16, C_BADGE, 200, rad=4)
                self.text.draw(dtxt, x + cw - 43, y + th - 17, 11)
            if idx == self.grid_idx and not self.in_sidebar:
                self._rect_border(x, y, cw, th, C_SEL, 4)
            self.text.draw(v.title, x, y + th + 3, 12, C_TEXT, max_w=cw)
            self.text.draw(v.channel or "...", x, y + th + 18, 11, C_DIM, max_w=cw)
        # indicador de "cargando mas" al fondo
        if self.loading_more:
            dots = "." * (1 + sdl2.SDL_GetTicks() // 300 % 3)
            self.text.draw(T("loading_more") + dots, gx + gw // 2 - 40,
                           UY + UH - 40, 12, (230, 200, 60))
        # status: cargas centradas con spinner, avisos abajo
        if self.status:
            if not self.videos and (self.status.startswith("Cargando")
                                    or self.status.startswith("Loading")
                                    or self.status.startswith("Buscando")
                                    or self.status.startswith("Searching")):
                self._draw_loading(self.status.rstrip("."),
                                   sdl2.SDL_GetTicks() // 300)
            else:
                self.text.draw(self.status, gx, UY + UH - 40, 12, (230, 200, 60))
        self._draw_help_bar()
        self._draw_overlays()

    def _draw_settings(self, gx, gw):
        """Lista de ajustes: etiqueta a la izquierda, valor a la derecha."""
        y = UY + 40
        for i, (key, label, value) in enumerate(self._settings_items()):
            sel = (i == self.settings_idx and not self.in_sidebar)
            if sel:
                self._fill_round(gx - 6, y - 5, gw - 6, 30, (55, 58, 70), rad=6)
            self.text.draw(label, gx, y, 14, C_TEXT if sel else C_DIM)
            # Las acciones se pintan en ambar: no cambian un valor, ejecutan.
            col = (230, 200, 60) if key in ("cache", "cookies") else C_TEXT
            vw = self.text.tex(value, 12, col)
            x = gx + gw - 18 - (vw[1] if vw else 0)
            self.text.draw(value, x, y + 3, 12, col)
            if sel and key not in ("cache", "cookies"):
                self.text.draw("<", gx + gw - 12, y + 3, 12, C_SEL)
            y += 34
        if self.status:
            self.text.draw(self.status, gx, UY + UH - 40, 12, (230, 200, 60))

    def _draw_help_bar(self):
        """Barra de ayuda. La anchura de cada pildora se calcula segun el
        texto: "START" no cabia en los 16 px pensados para una sola letra."""
        hb_y = UY + UH - 20
        hx = UX
        if self.section == "Settings":
            help_btns = [("A", T("hb_apply")), ("B", T("hb_back")),
                         ("< >", T("hb_change"))]
        else:
            help_btns = [("A", T("hb_play")), ("B", T("hb_quit")),
                         ("X", T("hb_search")), ("Y", T("hb_fav"))]
            if self.section == "Home":
                help_btns.append(("START", T("hb_refresh")))
        for btn, label in help_btns:
            w_btn = 16 if len(btn) == 1 else 10 + 7 * len(btn)
            self._fill_round(hx, hb_y, w_btn, 16, BTN_COLORS.get(btn, C_DIM),
                             rad=8)
            self.text.draw(btn, hx + 5, hb_y + 1, 11, (0, 0, 0))
            lw = self.text.draw(label, hx + w_btn + 4, hb_y + 1, 11, C_TEXT)
            hx += w_btn + 8 + max(40, (lw or 0) + 10)

    def _draw_overlays(self):
        # ventana de instrucciones de cookies
        if self.cookie_popup:
            mw, mh = 470, 250
            mx0 = UX + (UW - mw) // 2
            my0 = UY + (UH - mh) // 2
            self._fill(UX, UY, UW, UH, (0, 0, 0), 150)
            self._fill_round(mx0, my0, mw, mh, C_MODAL, 250, rad=12)
            t = COOKIE_TEXTS[self.cookie_lang]
            self.text.draw(t.get(self.cookie_popup, ""),
                           mx0 + 20, my0 + 14, 14, (230, 200, 60))
            y = my0 + 44
            for line in t["steps"]:
                self.text.draw(line, mx0 + 20, y, 12)
                y += 20
            self.text.draw(t["footer"], mx0 + mw - 160, my0 + mh - 24, 11, C_DIM)
        # modal
        if self.modal:
            q, opts, idx, _cb = self.modal
            mw, mh = 420, 130
            mx0 = UX + (UW - mw) // 2
            my0 = UY + (UH - mh) // 2
            self._fill(UX, UY, UW, UH, (0, 0, 0), 130)
            self._fill_round(mx0, my0, mw, mh, C_MODAL, 250, rad=12)
            self.text.draw(q, mx0 + 20, my0 + 18, 13, C_TEXT, max_w=mw - 40)
            for i, o in enumerate(opts):
                bx = mx0 + 40 + i * 180
                by = my0 + 70
                sel = (i == idx)
                self._fill_round(bx, by, 140, 32, C_SEL if sel else (90, 90, 100), rad=15)
                ow = self.text.tex(o, 13, C_TEXT)
                self.text.draw(o, bx + 70 - ((ow[1] if ow else 0) // 2),
                               by + 8, 13)

    def _render(self):
        self._draw_frame()
        sdl2.SDL_RenderPresent(self.ren)

    # ---------- main loop ----------
    def run(self):
        ev = sdl2.SDL_Event()
        while self.running:
            while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                if ev.type == sdl2.SDL_QUIT:
                    self.running = False
                elif ev.type == sdl2.SDL_CONTROLLERBUTTONDOWN:
                    b = self.BTN.get(ev.cbutton.button)
                    if b:
                        self.handle(b)
                elif ev.type == sdl2.SDL_CONTROLLERDEVICEADDED:
                    self._open_pad()
            self._render()
            sdl2.SDL_Delay(33)
        sdl2.SDL_Quit()


if __name__ == "__main__":
    App().run()
