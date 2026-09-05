"""YouTube port para R36S/R36T - reescritura en Python + PySDL2.
UI estilo original: sidebar, grid 2x2 de thumbnails, barra de ayuda."""
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


# Reproductores por orden de preferencia. mpv es el bueno, pero no viene en
# todas las imagenes (DarkOS 13 no lo trae), asi que hay un respaldo.
# ffplay queda descartado: no admite pista de audio separada (DASH sin
# sonido) y no expone control de buffer de red.
PLAYERS = ("mpv", "cvlc", "vlc")

# Directorios donde buscar el binario. BASE/bin va primero por si el port
# trae su propio mpv empaquetado.
PLAYER_DIRS = (os.path.join(BASE, "bin"), "/usr/bin", "/usr/local/bin", "/bin")


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _desktop_user():
    """Usuario normal al que degradar privilegios (VLC no arranca como root).

    El launcher hace 'sudo env ... python3 main.py', asi que SUDO_USER trae
    el usuario original. Si no, se busca el primer UID 1000 (ark en ArkOS
    y DarkOS)."""
    u = os.environ.get("SUDO_USER") or ""
    if u and u != "root":
        return u
    try:
        with open("/etc/passwd") as f:
            for line in f:
                p = line.split(":")
                if len(p) > 2 and p[2] == "1000":
                    return p[0]
    except OSError:
        pass
    return ""


def _find_exe(name):
    for d in PLAYER_DIRS:
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


def _drop_prefix(name):
    """Prefijo para lanzar VLC como usuario normal.

    VLC aborta con 'VLC is not supposed to be run as root', y el port corre
    como root porque PortMaster lo lanza con sudo. Devuelve None si no hay
    forma de degradar, para que ese reproductor se descarte."""
    if name not in ("vlc", "cvlc") or not _is_root():
        return []
    user = _desktop_user()
    sudo = _find_exe("sudo")
    if not user or not sudo:
        return None
    home = os.path.expanduser("~" + user)
    if not os.path.isdir(home):
        home = "/tmp"
    return [sudo, "-u", user, "env", "HOME=" + home]


def _which_player():
    """Primer reproductor disponible y utilizable en este entorno."""
    for name in CFG.get("video", {}).get("players", PLAYERS):
        path = _find_exe(name)
        if not path:
            continue
        if _drop_prefix(name) is None:
            continue   # VLC como root y sin forma de degradar: inservible
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


def _player_cmd(name, path, url, audio_url):
    """Linea de comandos del reproductor, con la pista de audio separada
    cuando el formato es DASH (audio_url no es None)."""
    extra = CFG.get("video", {}).get(name + "_args", [])
    if name == "mpv":
        cmd = [path, "--fs", "--no-terminal", "--really-quiet"]
        if audio_url:
            cmd.append("--audio-file=" + audio_url)
        return cmd + extra + [url]
    # vlc / cvlc. cvlc ya es un wrapper que hace "vlc -I dummy", asi que
    # solo hay que forzar la interfaz nula cuando se invoca vlc a secas.
    cmd = [path, "--play-and-exit", "--no-osd"]
    if name == "vlc":
        cmd[1:1] = ["-I", "dummy"]
    if audio_url:
        cmd.append("--input-slave=" + audio_url)
    return (_drop_prefix(name) or []) + cmd + extra + [url]


NO_PLAYER_TEXT = [
    "No se encontro ningun reproductor de video usable.",
    "",
    "Instala mpv en la consola (por SSH o terminal):",
    "  sudo apt install mpv",
    "",
    "VLC sirve de respaldo, pero se niega a correr como",
    "root: hace falta el usuario ark y sudo para bajarle",
    "los privilegios.",
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
              "X": (50, 90, 220), "Y": (40, 180, 70)}

SIDEBAR_ITEMS = ["Home", "Search", "Favorites", "History"]

DL_TEXTS = {
    "en": {
        "title": "Downloading yt-dlp (video engine)",
        "fail": "Download failed (no WiFi or GitHub down)",
        "manual": [
            "Manual install:",
            "1. Download the ARM64 binary of yt-dlp:",
            "   github.com/yt-dlp/yt-dlp/releases",
            "   (file: yt-dlp_linux_aarch64)",
            "2. Save it on the console as:",
            "   /roms/ports/youtube_py/yt-dlp.real",
            "3. Make it executable: chmod +x yt-dlp.real",
        ],
        "retry": "A: retry   B: continue without playback   Y: Espanol",
    },
    "es": {
        "title": "Descargando yt-dlp (motor de video)",
        "fail": "Fallo la descarga (sin WiFi o GitHub caido)",
        "manual": [
            "Instalacion manual:",
            "1. Descargar el binario ARM64 de yt-dlp:",
            "   github.com/yt-dlp/yt-dlp/releases",
            "   (archivo: yt-dlp_linux_aarch64)",
            "2. Guardarlo en la consola como:",
            "   /roms/ports/youtube_py/yt-dlp.real",
            "3. Permisos de ejecucion: chmod +x yt-dlp.real",
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

    def __init__(self):
        self.ready = {}      # video_id -> ruta de archivo descargado
        self.textures = {}   # video_id -> SDL texture
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
        vid = video.id
        if vid in self.textures:
            return self.textures[vid]
        with self.lock:
            path = self.ready.pop(vid, None)
        if path:
            tex = img.IMG_LoadTexture(ren, path.encode())
            if tex:
                self.textures[vid] = tex
                return tex
        return None


class App(object):
    def __init__(self):
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
        self.grid_idx = 0
        self.scroll_row = 0
        self.videos = []
        self.status = "Cargando feed..."
        self.cookie_warn = False
        self.cookie_popup = None   # 'missing' | 'expired' -> muestra ventana
        self.cookie_lang = "en"
        self.modal = None          # (pregunta, [opciones], idx, callback)
        self.running = True
        self.section = "Home"
        self.search_token = None      # continuacion de busqueda innertube
        self.feed_offset = 0          # paginacion del home
        self.loading_more = False
        self.favorites = self._load_json("favorites.json")
        self.history = self._load_json("history.json")

        if not backend.ytdlp_present():
            self._download_ytdlp()
        else:
            # en background: actualizar yt-dlp si hay version nueva
            def upd():
                v = backend.update_ytdlp_if_needed()
                if v:
                    self.status = "yt-dlp actualizado a %s" % v
            threading.Thread(target=upd, daemon=True).start()

        threading.Thread(target=self._load_home, daemon=True).start()

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
        self.thumbs.textures.clear()

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

    def _load_home(self):
        # 1) mostrar el ultimo feed cacheado al instante
        cached = self._load_json("feed_cache.json")
        if cached:
            self.videos = cached
            self.status = ""
            self._prefetch(cached)
        # 2) refrescar en segundo plano
        vids, cookie_state = backend.home_feed(CFG["search_count"] * 2)
        if vids:
            self.videos = vids
            self.status = ""
            self._save_json("feed_cache.json", vids)
            self._prefetch(vids)
        elif not cached:
            self.status = "Sin conexion o sin resultados - Home reintenta"
        if cookie_state != "ok":
            self.cookie_popup = cookie_state
        self._fill_channels(self.videos)

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
                    self.status = "Sin conexion WiFi"
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
                    self.status = "Sin conexion WiFi"
                finally:
                    self.loading_more = False
            threading.Thread(target=worker2, daemon=True).start()

    def _do_search(self, query):
        self.status = "Buscando..."
        self.videos = []
        self.grid_idx = 0
        self.scroll_row = 0

        def worker():
            try:
                self.videos, self.search_token = backend.search(
                    query, CFG["search_count"] * 2)
                self.status = "" if self.videos else "Sin resultados"
                self._prefetch(self.videos)
            except backend.NetworkError:
                self.status = "Sin conexion WiFi - revisa la red y reintenta (X)"
            except Exception as e:
                self.status = "Error: %s" % e
        threading.Thread(target=worker, daemon=True).start()

    # ---------- playback ----------
    def _install_mpv(self):
        """Descarga la dependencia mpv (~4 MB) tras confirmar con el usuario.

        No instala nada en el sistema: queda en youtube_py/{bin,lib} y solo
        la usa el reproductor. Devuelve True si mpv quedo disponible."""
        ev = sdl2.SDL_Event()
        choice = [None]
        info = [
            "Esta consola no trae ningun reproductor de video.",
            "",
            "Puedo descargar mpv (unos 4 MB) dentro de la",
            "carpeta del port. No se instala nada en el",
            "sistema y se borra quitando la carpeta.",
        ]
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
            self._modal_box("Falta un reproductor de video", info,
                            "A = descargar mpv    B = cancelar")
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
            self.status = "Sin conexion o video no disponible"
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
            proc = subprocess.Popen(
                _player_cmd(name, path, url, result.get("audio")),
                env=_player_env())
            # B = salir del video (mismo boton que atras en la app).
            was_down = True   # ignorar si B sigue pulsado al entrar
            while proc.poll() is None:
                sdl2.SDL_GameControllerUpdate()
                down = bool(self.pad and sdl2.SDL_GameControllerGetButton(
                    self.pad, sdl2.SDL_CONTROLLER_BUTTON_B))
                if down and not was_down:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                was_down = down
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
            self._confirm_exit()
        elif btn == "A":          # A = seleccionar/reproducir
            if self.in_sidebar:
                self._enter_section()
            elif self.videos:
                self.play(self.videos[self.grid_idx])
        elif btn == "Y":
            self._toggle_fav()
        elif btn == "X":
            self._enter_search()
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

    def _enter_section(self):
        self.section = SIDEBAR_ITEMS[self.sidebar_idx]
        self.in_sidebar = False
        self.grid_idx = 0
        self.scroll_row = 0
        if self.section == "Home":
            self.status = "Cargando feed..."
            self.videos = []
            self.feed_offset = 0
            threading.Thread(target=self._load_home, daemon=True).start()
        elif self.section == "Favorites":
            self.videos = list(self.favorites)
            self.status = "" if self.videos else "Sin favoritos"
        elif self.section == "History":
            self.videos = list(self.history)
            self.status = "" if self.videos else "Historial vacio"
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
        self.modal = ["Are you sure you want to exit?", ["No", "Yes"], 0,
                      lambda ans: setattr(self, "running", ans != "Yes")]

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
            self.text.draw(item, UX + 14, y, 13,
                           C_TEXT if sel else C_DIM)
        # cabecera
        gx = UX + sb_w + 12
        gw = UW - sb_w - 12
        self.text.draw(self.section, gx, UY, 20)
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
                        self.text.draw("sin imagen", x + cw // 2 - 30,
                                       y + th // 2 - 8, 11, C_DIM)
                    else:
                        dots = "." * (1 + sdl2.SDL_GetTicks() // 400 % 3)
                        self.text.draw("cargando" + dots, x + cw // 2 - 30,
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
            self.text.draw("cargando mas" + dots, gx + gw // 2 - 40,
                           UY + UH - 40, 12, (230, 200, 60))
        # status: cargas centradas con spinner, avisos abajo
        if self.status:
            if not self.videos and (self.status.startswith("Cargando")
                                    or self.status.startswith("Buscando")):
                self._draw_loading(self.status.rstrip("."),
                                   sdl2.SDL_GetTicks() // 300)
            else:
                self.text.draw(self.status, gx, UY + UH - 40, 12, (230, 200, 60))
        # help bar
        hb_y = UY + UH - 20
        hx = UX
        for btn, label in [("A", "Play"), ("B", "Quit"), ("X", "Search"), ("Y", "Fav")]:
            self._fill_round(hx, hb_y, 16, 16, BTN_COLORS[btn], rad=8)
            self.text.draw(btn, hx + 4, hb_y + 1, 11, (0, 0, 0))
            w = self.text.draw(label, hx + 20, hb_y + 1, 11, C_TEXT)
            hx += 20 + 8 + 60
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
            mw, mh = 380, 120
            mx0 = UX + (UW - mw) // 2
            my0 = UY + (UH - mh) // 2
            self._fill(UX, UY, UW, UH, (0, 0, 0), 130)
            self._fill_round(mx0, my0, mw, mh, C_MODAL, 250, rad=12)
            self.text.draw(q, mx0 + 24, my0 + 20, 14)
            for i, o in enumerate(opts):
                bx = mx0 + 40 + i * 170
                by = my0 + 64
                sel = (i == idx)
                self._fill_round(bx, by, 130, 32, C_SEL if sel else (90, 90, 100), rad=15)
                self.text.draw(o, bx + 52, by + 8, 13)

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
