"""Backend: busqueda rapida via Innertube (python puro) + yt-dlp para
feed personal y resolucion de streams. Python 3.7 compatible."""
import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
YTDLP = os.path.join(BASE, "yt-dlp.real")
if not os.path.exists(YTDLP):
    YTDLP = "/roms/ports/youtube/yt-dlp.real"
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


def _ytdlp_flat(url_or_query, limit, use_cookies, offset=0):
    cmd = [YTDLP, "--flat-playlist", "--dump-json", "--no-warnings",
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
        vid = j.get("id") or ""
        # Mixes: playlist RD<id> -> video semilla
        if vid.startswith("RD"):
            vid = vid[2:]
        if not vid:
            continue
        vids.append(Video(
            id=vid, title=j.get("title") or "?",
            channel=j.get("channel") or j.get("uploader") or "",
            thumbnail="",  # usar mqdefault (320x180): 8x mas ligero que hq720
            duration=int(j.get("duration") or 0),
            view_count=int(j.get("view_count") or 0)))
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


def resolve_stream(video, quality=480):
    """URL de stream. Sin cookies (PO token). Mixes ya normalizados en Video."""
    fmt = ("best[height<=%d][ext=mp4]/best[height<=%d][acodec!=none][vcodec!=none]"
           "/18/22/best[height<=%d]/best" % (quality, quality, quality))
    cmd = [YTDLP, "-f", fmt, "-g", "--no-warnings", "--no-check-certificates",
           "--no-playlist", "--extractor-args", "youtube:player_client=android,web",
           video.url]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=60).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    lines = out.decode(errors="replace").strip().splitlines()
    return lines[0] if lines else None


def channel_of(video_id):
    """Nombre del canal via Innertube player (rapido, sin yt-dlp)."""
    try:
        data = _http_post(
            "https://www.youtube.com/youtubei/v1/player?key=" + INNERTUBE_KEY,
            {"context": INNERTUBE_CTX, "videoId": video_id}, timeout=6)
        return data.get("videoDetails", {}).get("author", "")
    except Exception:
        return ""


YTDLP_URL = ("https://github.com/yt-dlp/yt-dlp/releases/latest/"
             "download/yt-dlp_linux_aarch64")


def ytdlp_present():
    return os.path.exists(YTDLP)


def download_ytdlp(progress_cb=None):
    """Descarga yt-dlp (~36 MB) a BASE/yt-dlp.real con progreso 0-100.
    Usa curl (TLS del sistema); lanza NetworkError si falla."""
    global YTDLP
    dest = os.path.join(BASE, "yt-dlp.real")
    part = dest + ".part"
    try:
        os.remove(part)
    except OSError:
        pass
    est_total = 38 * 1024 * 1024   # estimacion si no hay Content-Length
    proc = subprocess.Popen(
        ["curl", "-sS", "-L", "--insecure", "--max-time", "600",
         "--connect-timeout", "15", "-o", part, YTDLP_URL],
        stderr=subprocess.DEVNULL)
    while proc.poll() is None:
        if progress_cb:
            try:
                done = os.path.getsize(part)
            except OSError:
                done = 0
            progress_cb(min(99, done * 100 // est_total))
        import time
        time.sleep(0.5)
    ok = proc.returncode == 0
    if ok:
        try:
            ok = os.path.getsize(part) >= 10 * 1024 * 1024
        except OSError:
            ok = False
    if not ok:
        try:
            os.remove(part)
        except OSError:
            pass
        raise NetworkError("descarga fallida (red bloqueada o sin conexion)")
    if progress_cb:
        progress_cb(100)
    os.replace(part, dest)
    os.chmod(dest, 0o755)
    YTDLP = dest
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def latest_ytdlp_version():
    """Version mas reciente publicada, leyendo el redirect de GitHub."""
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
    try:
        out = subprocess.run([YTDLP, "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=30).stdout
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

