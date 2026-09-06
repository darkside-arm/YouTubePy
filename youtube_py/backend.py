"""Backend: busqueda rapida via Innertube (python puro) + yt-dlp para
feed personal y resolucion de streams. Python 3.7 compatible."""
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
YTDLP = os.path.join(BASE, "yt-dlp.real")
if not os.path.exists(YTDLP):
    YTDLP = "/roms/ports/youtube/yt-dlp.real"
# Zipapp de yt-dlp (python puro, ~3 MB). Se prefiere al binario PyInstaller
# de 36 MB porque se puede importar DENTRO de este proceso: el binario paga
# ~6 s de arranque en CADA invocacion (descomprimir el archivo + levantar un
# interprete), mientras que el zipapp se importa una sola vez al arrancar.
YTDLP_ZIP = os.path.join(BASE, "yt-dlp.zip")
COOKIES = os.path.join(BASE, "cookies.txt")
if not os.path.exists(COOKIES):
    COOKIES = "/roms/ports/youtube/cookies.txt"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
INNERTUBE_CTX = {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}}


class Video(object):
    __slots__ = ("id", "title", "channel", "thumbnail", "duration", "view_count", "is_notice")

    def __init__(self, id="", title="", channel="", thumbnail="", duration=0,
                 view_count=0, is_notice=False):
        self.id = id
        self.title = title
        self.channel = channel
        self.thumbnail = thumbnail or ("https://i.ytimg.com/vi/%s/mqdefault.jpg" % id)
        self.duration = duration or 0
        self.view_count = view_count or 0
        self.is_notice = is_notice

    @property
    def url(self):
        return "https://www.youtube.com/watch?v=" + self.id


class NetworkError(Exception):
    pass


def _http_post(url, payload, timeout=10):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError) as e:
        raise NetworkError(str(e))


def _walk(node, key, out):
    if isinstance(node, dict):
        if key in node:
            out.append(node[key])
        for v in node.values():
            _walk(v, key, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, key, out)


def _text(node):
    if not node:
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs", []))


def _parse_duration(s):
    parts = s.split(":")
    try:
        sec = 0
        for p in parts:
            sec = sec * 60 + int(p)
        return sec
    except ValueError:
        return 0


def search(query, limit=20):
    """Busqueda via Innertube. Devuelve (videos, token_continuacion)."""
    data = _http_post(
        "https://www.youtube.com/youtubei/v1/search?key=" + INNERTUBE_KEY,
        {"context": INNERTUBE_CTX, "query": query})
    return _parse_search(data, limit)


def search_continue(token, limit=20):
    """Siguiente pagina de una busqueda. Devuelve (videos, token)."""
    data = _http_post(
        "https://www.youtube.com/youtubei/v1/search?key=" + INNERTUBE_KEY,
        {"context": INNERTUBE_CTX, "continuation": token})
    return _parse_search(data, limit)


def _parse_search(data, limit):
    found = []
    _walk(data, "videoRenderer", found)
    vids = []
    for v in found[:limit]:
        try:
            vids.append(Video(
                id=v["videoId"],
                title=_text(v.get("title")),
                channel=_text(v.get("ownerText") or v.get("longBylineText")),
                duration=_parse_duration(_text(v.get("lengthText"))),
                view_count=0,
            ))
        except (KeyError, TypeError):
            continue
    tokens = []
    _walk(data, "continuationCommand", tokens)
    token = tokens[0].get("token") if tokens else None
    return vids, token


# ---------- motor yt-dlp en proceso ----------
# Importar el zipapp dentro de este proceso cuesta ~6 s una sola vez (se hace
# en segundo plano al arrancar la app, tapado por la carga del feed) y ahorra
# esos mismos ~6 s en CADA resolucion posterior.
_YDL_LOCK = threading.Lock()
_YDL_CACHE = {}          # clave -> instancia YoutubeDL
_YDL_MODULE = [None]     # [modulo yt_dlp] o [False] si no se puede usar


def _ytdlp_module():
    """Importa yt_dlp desde el zipapp. Devuelve el modulo o None."""
    if _YDL_MODULE[0] is not None:
        return _YDL_MODULE[0] or None
    # El zipapp lleva dentro un guardia explicito que aborta con ImportError
    # en versiones viejas (ver YTDLP_MIN_PY); ahi toca el binario.
    if not _use_zipapp() or not os.path.exists(YTDLP_ZIP):
        _YDL_MODULE[0] = False
        return None
    try:
        if YTDLP_ZIP not in sys.path:
            sys.path.insert(0, YTDLP_ZIP)
        import yt_dlp                      # noqa: PLC0415
        _YDL_MODULE[0] = yt_dlp
        return yt_dlp
    except Exception:                      # noqa: BLE001
        _YDL_MODULE[0] = False
        return None


def _get_ydl(key, opts):
    """Instancia YoutubeDL cacheada (construirla cuesta ~3 s)."""
    ydl = _YDL_CACHE.get(key)
    if ydl is not None:
        return ydl
    mod = _ytdlp_module()
    if mod is None:
        return None
    base = {"quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "skip_download": True, "socket_timeout": 15,
            "noprogress": True, "logger": _NullLogger()}
    base.update(opts)
    try:
        ydl = mod.YoutubeDL(base)
    except Exception:                      # noqa: BLE001
        return None
    _YDL_CACHE[key] = ydl
    return ydl


class _NullLogger(object):
    """yt-dlp escribe por stdout/stderr; aqui eso ensucia log.txt."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def _stream_opts(quality, hq=True):
    """Opciones de resolucion de stream.

    hq=True: formatos DASH, que es donde vive el 480p de verdad. Se exige
    avc1 (H.264) porque es lo unico que esta CPU decodifica con holgura; vp9
    y av1 a 480p no van. Video y audio vienen separados y el reproductor los
    junta con --audio-file.

    hq=False: respaldo con el cliente android, que entrega un progresivo
    (itag 18, 640x360) ya firmado. Se usa cuando el camino DASH falla, p. ej.
    en videos con restriccion de edad."""
    if hq:
        fmt = ("bv*[height<=%d][vcodec^=avc1]+ba[acodec^=mp4a]"
               "/bv*[height<=%d][vcodec^=avc1]+ba"
               "/b[height<=%d][ext=mp4]/18/b" % (quality, quality, quality))
        return {"format": fmt, "noplaylist": True}
    return {
        "format": _stream_format(quality),
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }


def warmup_ytdlp(quality=480):
    """Precarga el motor para que la primera reproduccion no pague el arranque.

    Pensado para lanzarse en un hilo al abrir la app. Devuelve True si el
    motor en proceso quedo listo."""
    with _YDL_LOCK:
        return _get_ydl("stream:hq:%d" % quality,
                        _stream_opts(quality, True)) is not None


def ytdlp_inprocess():
    return bool(_YDL_MODULE[0])


def _ytdlp_cmd():
    """Prefijo de comando para invocar yt-dlp como subproceso."""
    if os.path.exists(YTDLP_ZIP) and _use_zipapp():
        return [sys.executable, YTDLP_ZIP]
    return [YTDLP]


def _video_from_entry(j):
    """Construye un Video a partir de una entrada plana de yt-dlp."""
    vid = j.get("id") or ""
    # Mixes: playlist RD<id> -> video semilla
    if vid.startswith("RD"):
        vid = vid[2:]
    if not vid:
        return None
    return Video(
        id=vid, title=j.get("title") or "?",
        channel=j.get("channel") or j.get("uploader") or "",
        thumbnail="",  # usar mqdefault (320x180): 8x mas ligero que hq720
        duration=int(j.get("duration") or 0),
        view_count=int(j.get("view_count") or 0))


def _flat_inprocess(url_or_query, limit, use_cookies, offset):
    """Listado plano con el modulo ya importado. None si no esta disponible.

    Evita lanzar un subproceso, que en esta CPU cuesta ~6 s y satura los 4
    nucleos justo cuando se estan bajando las miniaturas."""
    cookies = use_cookies and os.path.exists(COOKIES)
    # Una sola instancia por modo (con o sin cookies). El rango se cambia
    # mutando params en cada llamada: cachear una instancia por cada offset
    # iria acumulando objetos YoutubeDL segun se pagina.
    key = "flat:%s" % bool(cookies)
    with _YDL_LOCK:
        opts = {"extract_flat": "in_playlist", "ignoreerrors": True}
        if cookies:
            opts["cookiefile"] = COOKIES
        ydl = _get_ydl(key, opts)
        if ydl is None:
            return None
        ydl.params["playlist_items"] = "%d-%d" % (offset + 1, offset + limit)
        try:
            info = ydl.extract_info(url_or_query, download=False)
        except Exception:                  # noqa: BLE001
            return None
    if not info:
        return None
    vids = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        v = _video_from_entry(entry)
        if v:
            vids.append(v)
    return vids


def _ytdlp_flat(url_or_query, limit, use_cookies, offset=0):
    r = _flat_inprocess(url_or_query, limit, use_cookies, offset)
    if r is not None:
        return r
    cmd = _ytdlp_cmd() + [
        "--flat-playlist", "--dump-json", "--no-warnings",
        "--ignore-errors", "--no-check-certificates",
        "--playlist-items", "%d-%d" % (offset + 1, offset + limit),
        "--socket-timeout", "15"]
    if use_cookies and os.path.exists(COOKIES):
        cmd += ["--cookies", COOKIES]
    cmd.append(url_or_query)
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=60).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    vids = []
    for line in out.decode(errors="replace").splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        v = _video_from_entry(j)
        if v:
            vids.append(v)
    return vids


def home_feed(limit=20, offset=0):
    """Devuelve (videos, estado_cookies): 'ok', 'missing' o 'expired'."""
    if os.path.exists(COOKIES):
        vids = _ytdlp_flat(":ytrec", limit, use_cookies=True, offset=offset)
        if vids:
            return vids, "ok"
        if offset:
            return [], "ok"
        return _ytdlp_flat("ytsearch%d:trending music" % limit, limit, False), "expired"
    return _ytdlp_flat("ytsearch%d:trending music" % limit, limit, False), "missing"


def _stream_format(quality):
    return ("best[height<=%d][ext=mp4]/best[height<=%d][acodec!=none][vcodec!=none]"
            "/18/22/best[height<=%d]/best" % (quality, quality, quality))


def resolve_stream(video, quality=480):
    """URL(s) de stream. Sin cookies (PO token). Mixes ya normalizados en Video.

    Devuelve (video_url, audio_url). Con DASH (el caso normal, que es donde
    hay 480p real) vienen video y audio separados y hay que pasar los dos al
    reproductor: quedarse solo con el primero da reproduccion muda.

    Tres intentos, de mejor a mas seguro:
      1. DASH en proceso   -> 854x480 avc1 + audio m4a  (4-7 s)
      2. progresivo android en proceso -> 640x360 itag 18, para videos que
         el camino DASH rechaza (restriccion de edad, etc.)
      3. subproceso, por si el motor en proceso no esta disponible."""
    r = _resolve_inprocess(video, quality, hq=True)
    if r is not None:
        return r
    r = _resolve_inprocess(video, quality, hq=False)
    if r is not None:
        return r
    # Sin motor en proceso (Python < 3.9): subproceso, tambien probando
    # primero el DASH de 480p y cayendo al progresivo de 360p.
    for hq in (True, False):
        r = _resolve_subprocess(video, quality, hq)
        if r is not None:
            return r
    return None, None


def _resolve_subprocess(video, quality, hq):
    """Resolucion lanzando yt-dlp como proceso. None si falla."""
    opts = _stream_opts(quality, hq)
    cmd = _ytdlp_cmd() + [
        "-f", opts["format"], "-g", "--no-warnings",
        "--no-check-certificates", "--no-playlist"]
    if not hq:
        cmd += ["--extractor-args", "youtube:player_client=android,web"]
    cmd.append(video.url)
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=90).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    lines = out.decode(errors="replace").strip().splitlines()
    lines = [l for l in lines if l.startswith("http")]
    if not lines:
        return None
    return lines[0], (lines[1] if len(lines) > 1 else None)


def _resolve_inprocess(video, quality, hq=True):
    """Resolucion con el modulo importado. None si no esta disponible."""
    key = "stream:%s:%d" % ("hq" if hq else "lq", quality)
    with _YDL_LOCK:
        ydl = _get_ydl(key, _stream_opts(quality, hq))
        if ydl is None:
            return None
        try:
            info = ydl.extract_info(video.url, download=False)
        except Exception:                  # noqa: BLE001
            return None
    if not info:
        return None
    # DASH: yt-dlp deja los dos formatos elegidos en requested_formats.
    req = info.get("requested_formats")
    if req:
        vurl = aurl = None
        for f in req:
            if f.get("vcodec") not in (None, "none") and not vurl:
                vurl = f.get("url")
            elif not aurl:
                aurl = f.get("url")
        if vurl:
            return vurl, aurl
    url = info.get("url")
    return (url, None) if url else None


def channel_of(video_id):
    """Nombre del canal via Innertube player (rapido, sin yt-dlp)."""
    try:
        data = _http_post(
            "https://www.youtube.com/youtubei/v1/player?key=" + INNERTUBE_KEY,
            {"context": INNERTUBE_CTX, "videoId": video_id}, timeout=6)
        return data.get("videoDetails", {}).get("author", "")
    except Exception:
        return ""


# Descarga directa desde el repo oficial de yt-dlp.
YTDLP_BASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download"
YTDLP_URL = YTDLP_BASE + "/yt-dlp_linux_aarch64"
YTDLP_ZIP_URL = YTDLP_BASE + "/yt-dlp"      # zipapp python puro (~3 MB)
YTDLP_SUMS_URL = YTDLP_BASE + "/SHA2-256SUMS"


# Version minima de Python que acepta el zipapp de yt-dlp. No es una
# limitacion de sintaxis: yt_dlp/__init__.py trae un guardia explicito que
# lanza ImportError ("Only Python versions 3.10 and above are supported").
# Comprobado en la XiFan XF40H (Ubuntu 19.10, Python 3.7.5). Por debajo de
# esto hay que usar el binario PyInstaller, que lleva su propio interprete.
YTDLP_MIN_PY = (3, 10)


def _use_zipapp():
    """True si este Python puede con el zipapp (~3 MB); si no, binario."""
    return sys.version_info >= YTDLP_MIN_PY


def ytdlp_present():
    return os.path.exists(YTDLP_ZIP) or os.path.exists(YTDLP)


def _curl_text(url, timeout=15):
    """Devuelve el cuerpo de una URL con curl, o None si falla."""
    try:
        out = subprocess.run(
            ["curl", "-sS", "-L", "--insecure", "--max-time", str(timeout),
             "--connect-timeout", "10", url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout + 5)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    try:
        return out.stdout.decode().strip()
    except UnicodeDecodeError:
        return None


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_binary(url, part, expect_sha=None, progress_cb=None, resume=False,
                  minsize=10 * 1024 * 1024):
    """Un intento de descarga a `part`. True si quedo integro."""
    import time
    est_total = max(minsize, 1) * 11 // 10   # estimacion sin Content-Length
    cmd = ["curl", "-sS", "-L", "--insecure", "--max-time", "600",
           "--connect-timeout", "15", "--retry", "3", "--retry-delay", "2",
           "--speed-time", "45", "--speed-limit", "1024"]
    if resume:
        cmd += ["-C", "-"]
    cmd += ["-o", part, url]
    proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
    while proc.poll() is None:
        if progress_cb:
            try:
                done = os.path.getsize(part)
            except OSError:
                done = 0
            progress_cb(min(99, done * 100 // est_total))
        time.sleep(0.5)
    if proc.returncode != 0:
        return False
    try:
        if os.path.getsize(part) < minsize:
            return False
    except OSError:
        return False
    if expect_sha:
        try:
            if _sha256_file(part).lower() != expect_sha.lower():
                return False
        except OSError:
            return False
    return True


def _upstream_sha256(name="yt-dlp_linux_aarch64"):
    """SHA-256 oficial de un asset de yt-dlp segun SHA2-256SUMS."""
    body = _curl_text(YTDLP_SUMS_URL, timeout=20)
    if not body:
        return None
    for line in body.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == "yt-dlp_linux_aarch64":
            if len(parts[0]) == 64:
                return parts[0]
    return None


def download_ytdlp(progress_cb=None):
    """Descarga yt-dlp del repo oficial con progreso 0-100.

    Con Python 3.9+ baja el zipapp (~3 MB) a BASE/yt-dlp.zip, que ademas se
    puede importar en proceso; en Python mas viejo, el binario PyInstaller
    (~36 MB) a BASE/yt-dlp.real. Verifica el SHA-256 contra el SHA2-256SUMS
    publicado y reintenta reanudando descargas cortadas.
    Lanza NetworkError si todos los intentos fallan."""
    global YTDLP
    zipapp = _use_zipapp()
    if zipapp:
        dest, url, name, minsize = YTDLP_ZIP, YTDLP_ZIP_URL, "yt-dlp", 1000000
    else:
        dest = os.path.join(BASE, "yt-dlp.real")
        url, name, minsize = YTDLP_URL, "yt-dlp_linux_aarch64", 10000000
    part = dest + ".part"

    sha = _upstream_sha256(name)

    # (sha esperado, reanudar) en orden de preferencia
    attempts = [
        (sha, False),
        (sha, True),      # reanuda el .part cortado
        (sha, False),     # ultimo intento desde cero
    ]

    for expect, resume in attempts:
        if not resume:
            try:
                os.remove(part)
            except OSError:
                pass
        if _fetch_binary(url, part, expect, progress_cb, resume, minsize):
            if progress_cb:
                progress_cb(100)
            os.replace(part, dest)
            os.chmod(dest, 0o755)
            if not zipapp:
                YTDLP = dest
            _YDL_MODULE[0] = None      # reintentar el import con el nuevo zip
            _YDL_CACHE.clear()
            return True

    try:
        os.remove(part)
    except OSError:
        pass
    raise NetworkError("descarga fallida (red bloqueada o sin conexion)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# ---------- dependencia opcional: mpv ----------
# Paquete con mpv y las librerias que suelen faltar, publicado como asset del
# release. No viaja dentro del zip del port: se descarga solo si la consola
# no trae ningun reproductor usable.
MPV_ZIP_URL = ("https://github.com/darkside-arm/YouTubePy/releases/"
               "download/ytdlp-latest/mpv-aarch64.zip")
MPV_BIN = os.path.join(BASE, "bin", "mpv")


def mpv_present():
    return os.access(MPV_BIN, os.X_OK)


def download_mpv(progress_cb=None):
    """Descarga y extrae la dependencia mpv en BASE/{bin,lib}.

    No instala nada en el sistema: el binario y sus librerias quedan dentro
    de la carpeta del port y solo los usa el reproductor (via
    LD_LIBRARY_PATH). Lanza NetworkError si falla."""
    import zipfile
    part = os.path.join(BASE, "mpv-aarch64.zip.part")
    try:
        os.remove(part)
    except OSError:
        pass
    est_total = 4 * 1024 * 1024
    if not _fetch_binary_generic(MPV_ZIP_URL, part, est_total, progress_cb):
        try:
            os.remove(part)
        except OSError:
            pass
        raise NetworkError("no se pudo descargar mpv")
    try:
        with zipfile.ZipFile(part) as z:
            z.extractall(BASE)
    except (zipfile.BadZipFile, OSError) as e:
        try:
            os.remove(part)
        except OSError:
            pass
        raise NetworkError("paquete mpv corrupto: %s" % e)
    try:
        os.remove(part)
    except OSError:
        pass
    try:
        os.chmod(MPV_BIN, 0o755)
    except OSError:
        pass
    if progress_cb:
        progress_cb(100)
    return mpv_present()


def _fetch_binary_generic(url, part, est_total, progress_cb=None):
    """Descarga con curl mostrando progreso estimado. True si parece integra."""
    import time
    proc = subprocess.Popen(
        ["curl", "-sS", "-L", "--insecure", "--max-time", "600",
         "--connect-timeout", "15", "--retry", "3", "--retry-delay", "2",
         "--speed-time", "45", "--speed-limit", "1024", "-o", part, url],
        stderr=subprocess.DEVNULL)
    while proc.poll() is None:
        if progress_cb:
            try:
                done = os.path.getsize(part)
            except OSError:
                done = 0
            progress_cb(min(99, done * 100 // est_total))
        time.sleep(0.5)
    if proc.returncode != 0:
        return False
    try:
        return os.path.getsize(part) > 100 * 1024
    except OSError:
        return False


def latest_ytdlp_version():
    """Version mas reciente publicada en el repo oficial de yt-dlp,
    leyendo el redirect de GitHub."""
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=SSL_CTX))
    req = urllib.request.Request(
        "https://github.com/yt-dlp/yt-dlp/releases/latest",
        headers={"User-Agent": "Mozilla/5.0"})
    try:
        opener.open(req, timeout=10)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        if "/tag/" in loc:
            return loc.rsplit("/tag/", 1)[1]
    except (urllib.error.URLError, OSError):
        pass
    return None


def current_ytdlp_version():
    # Si el modulo ya esta importado, la version se lee sin lanzar procesos.
    mod = _YDL_MODULE[0]
    if mod:
        try:
            return mod.version.__version__
        except AttributeError:
            pass
    try:
        out = subprocess.run(_ytdlp_cmd() + ["--version"],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=60).stdout
        return out.decode().strip() or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def update_ytdlp_if_needed():
    """Si hay una version mas nueva, la descarga y reemplaza (atomico).
    Devuelve la version nueva si actualizo, si no None."""
    if not ytdlp_present():
        return None
    latest = latest_ytdlp_version()
    if not latest:
        return None
    current = current_ytdlp_version()
    if current == latest:
        return None
    try:
        download_ytdlp()
        return latest
    except NetworkError:
        return None


def fetch_thumbnail(url, dest, timeout=8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            data = r.read()
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False

