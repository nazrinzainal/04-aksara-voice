"""Aksara Voice - a small, local neural voice studio.

Run:  python app.py [--no-browser] [--port 7788]
Open: http://127.0.0.1:7788
"""
from __future__ import annotations

import errno
import json
import math
import re
import select
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tts_engine import (WEB_DIR, Cancelled, EngineError, History, TTSEngine,
                        load_config, safe_audio_path)

VERSION = "2.3"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 256 * 1024  # bytes; max_chars (<= 8000) of UTF-8 fits easily
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self' blob:; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'")

# name -> (minimum, maximum, default)
NUMBER_FIELDS = {
    "speed": (0.7, 1.3, 1.0),
    "pitch": (-20, 20, 0),
    "exaggeration": (0.2, 0.9, 0.5),
    "stability": (0.0, 1.0, 0.5),
}


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def log(*parts):
    print(time.strftime("[%H:%M:%S]"), *parts, flush=True)


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def parse_tts_body(body) -> dict:
    """Validate a /api/tts body. Raises BadRequest with a clear message."""
    if not isinstance(body, dict):
        raise BadRequest("Body mesti objek JSON.")
    text = body.get("text")
    if not isinstance(text, str):
        raise BadRequest("Medan 'text' mesti teks.")
    voice = body.get("voice")
    if not isinstance(voice, str) or not voice.strip():
        raise BadRequest("Medan 'voice' diperlukan.")
    out = {"text": text, "voice": voice.strip()}
    for name, (lo, hi, default) in NUMBER_FIELDS.items():
        value = body.get(name, default)
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise BadRequest(f"Medan '{name}' mesti nombor.")
        if not lo <= value <= hi:
            raise BadRequest(f"Medan '{name}' mesti antara {lo} dan {hi}.")
        out[name] = value
    out["pitch"] = int(round(out["pitch"]))
    save = body.get("save", True)
    if not isinstance(save, bool):
        raise BadRequest("Medan 'save' mesti true atau false.")
    out["save"] = save
    return out


def parse_range(header: str | None, size: int):
    """Return (start, end) for a single satisfiable byte range, None to send the
    whole file, or "unsatisfiable" for a 416."""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
    if not m or (not m.group(1) and not m.group(2)):
        return None  # malformed or multi-range: ignore and send the full file (RFC 9110)
    if size <= 0:
        return "unsatisfiable"
    if not m.group(1):  # suffix range: last N bytes
        n = int(m.group(2))
        if n <= 0:
            return "unsatisfiable"
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        return "unsatisfiable"
    return start, min(end, size - 1)


class App:
    def __init__(self):
        self.cfg = load_config()
        self.engine = TTSEngine(self.cfg)
        self.history = History(int(self.cfg["history_limit"]))

    def catalog(self) -> dict:
        data = self.engine.catalog()
        data["version"] = VERSION
        return data


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AksaraVoice/" + VERSION
        timeout = 120  # socket timeout for reading requests / writing responses

        def log_message(self, fmt, *args):
            pass

        # ------------------------------------------------------------------ helpers
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
                  head: bool = False):
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if not head:
                    self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
                pass

        def _json(self, code: int, obj: dict, head: bool = False):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", head=head)

        def _error(self, code: int, message: str):
            self._json(code, {"error": message})

        def _host_ok(self) -> bool:
            """Block DNS-rebinding: only accept loopback Host headers."""
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return True
            if host.startswith("["):
                name = host[1:host.find("]")] if "]" in host else host
            else:
                name = host.rsplit(":", 1)[0] if ":" in host else host
            return name in LOOPBACK

        def _origin_ok(self) -> bool:
            """Block cross-site POSTs (another website poking the local API)."""
            if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True  # non-browser clients (curl, tests) send no Origin
            try:
                parsed = urlparse(origin)
            except ValueError:
                return False
            port = self.server.server_address[1]
            return parsed.scheme == "http" and parsed.hostname in LOOPBACK and parsed.port == port

        def _read_json(self, required: bool = True):
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                raise BadRequest("Chunked body tidak disokong; hantar Content-Length.", 411)
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                if required:
                    raise BadRequest("Content-Length diperlukan.", 411)
                return {}
            try:
                n = int(raw_len)
            except ValueError:
                raise BadRequest("Content-Length tidak sah.")
            if n < 0:
                raise BadRequest("Content-Length tidak sah.")
            if n > MAX_BODY:
                raise BadRequest(f"Body terlalu besar (maks {MAX_BODY // 1024} KB).", 413)
            if n == 0:
                if required:
                    raise BadRequest("Body JSON diperlukan.")
                return {}
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise BadRequest("Content-Type mesti application/json.", 415)
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
            except (ValueError, UnicodeDecodeError):
                raise BadRequest("JSON tidak sah.")

        def _client_gone(self) -> bool:
            """True when the browser aborted the request (socket closed/reset)."""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if not readable:
                    return False
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                return True

        def _static(self, name: str, ctype: str, head: bool = False):
            path = WEB_DIR / name
            if not path.is_file():
                return self._error(404, "not found")
            extra = {"Referrer-Policy": "no-referrer"}
            if name.endswith(".html"):
                extra["Content-Security-Policy"] = CSP
                extra["X-Frame-Options"] = "DENY"
            self._send(200, path.read_bytes(), ctype, extra, head=head)

        def _send_audio(self, result: dict, item: dict | None = None):
            extra = {
                "Accept-Ranges": "bytes",
                "X-Duration": f'{result["duration"]:.2f}',
                "X-Gen-Seconds": f'{result["seconds"]:.2f}',
                "X-Engine": result["voice"]["engine"],
                "X-Voice": result["voice"]["id"],
                "X-Voice-Name": result["voice"]["name"],
                "X-Lang": result["voice"]["lang"],
                "X-Ext": result["ext"],
                "X-Chars": str(result["chars"]),
            }
            if item:
                extra["X-History-Id"] = item["id"]
                extra["X-Audio-Url"] = item["url"]
            self._send(200, result["data"], result["mime"], extra)

        # ------------------------------------------------------------------ routing
        def _dispatch(self, method: str):
            try:
                if not self._host_ok():
                    return self._error(403, "Host tidak dibenarkan.")
                url = urlparse(self.path)
                if method in ("GET", "HEAD"):
                    return self._get(url, head=method == "HEAD")
                if not self._origin_ok():
                    return self._error(403, "Permintaan dari laman lain tidak dibenarkan.")
                return self._post(url)
            except BadRequest as ex:
                return self._error(ex.status, str(ex))
            except Exception:  # never leak a traceback to the client
                log("server error:", self.command, self.path)
                traceback.print_exc()
                return self._error(500, "Ralat dalaman pelayan. Lihat log Aksara Voice.")

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def _method_not_allowed(self):
            self._error(405, "Method tidak dibenarkan.")

        do_PUT = do_DELETE = do_PATCH = _method_not_allowed

        # ---------------------------------------------------------------------- GET
        def _get(self, url, head: bool = False):
            if url.path == "/api/catalog":
                return self._json(200, app.catalog(), head=head)
            if url.path == "/api/health":
                return self._json(200, {"ok": True, "version": VERSION}, head=head)
            if url.path == "/api/history":
                q = parse_qs(url.query)
                raw = (q.get("limit") or ["30"])[0]
                try:
                    limit = max(1, min(100, int(raw)))
                except ValueError:
                    raise BadRequest("Parameter 'limit' mesti nombor bulat.")
                return self._json(200, {"items": app.history.list(limit)}, head=head)
            if url.path.startswith("/audio/"):
                return self._audio(url.path[len("/audio/"):], head=head)
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                return self._static(name, ctype, head=head)
            return self._error(404, "not found")

        def _audio(self, name: str, head: bool = False):
            path = safe_audio_path(name)
            if path is None or not path.is_file():
                return self._error(404, "not found")
            size = path.stat().st_size
            ctype = "audio/mpeg" if path.suffix == ".mp3" else "audio/wav"
            rng = parse_range(self.headers.get("Range"), size)
            if rng == "unsatisfiable":
                try:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
                    pass
                return
            start, end = rng if rng else (0, size - 1)
            length = max(0, end - start + 1)
            try:
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if head:
                    return
                with path.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
                pass

        # --------------------------------------------------------------------- POST
        def _post(self, url):
            if url.path == "/api/tts":
                return self._tts()
            if url.path == "/api/history/delete":
                body = self._read_json()
                if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"]:
                    raise BadRequest("Medan 'id' diperlukan.")
                ok = app.history.delete(body["id"])
                return self._json(200 if ok else 404, {"ok": ok} if ok else {"ok": False, "error": "Tidak dijumpai."})
            if url.path == "/api/history/clear":
                self._read_json(required=False)
                app.history.clear()
                return self._json(200, {"ok": True})
            return self._error(404, "not found")

        def _tts(self):
            params = parse_tts_body(self._read_json())
            last_check = [0.0, False]

            def cancelled() -> bool:
                now = time.monotonic()
                if now - last_check[0] > 0.25:
                    last_check[0] = now
                    last_check[1] = self._client_gone()
                return last_check[1]

            try:
                result = app.engine.synth(params["voice"], params["text"], params["speed"], params["pitch"],
                                          params["exaggeration"], params["stability"], cancelled=cancelled)
            except Cancelled:
                log("tts cancelled by client:", params["voice"])
                return None
            except EngineError as ex:
                log("tts error:", ex)
                return self._error(ex.status, str(ex))
            if self._client_gone():
                log("tts finished but client left; not saved:", result["voice"]["name"])
                return None
            item = None
            if params["save"]:
                item = app.history.add(result, result["text"], params["speed"], params["pitch"],
                                       result["exaggeration"], result["stability"])
            log(result["voice"]["engine"], result["voice"]["name"], f'{result["chars"]} chars',
                f'{result["duration"]:.1f}s audio', f'{result["seconds"]:.1f}s gen',
                "saved" if item else "preview")
            return self._send_audio(result, item)

    return Handler


class Server(ThreadingHTTPServer):
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def disable_power_throttling() -> bool:
    """Ask Windows 11 not to run this process in EcoQoS / "Efficiency mode".

    A server started from a shortcut has no visible window and its launcher exits, so Windows treats
    it as background work and parks its threads on efficiency cores at low clock: Chatterbox measured
    ~2.8 tokens/s instead of ~13. Opting out (EXECUTION_SPEED + IGNORE_TIMER_RESOLUTION, state 0)
    restores full speed. Best effort; no-op on other systems."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _State(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetProcessInformation.restype = wintypes.BOOL
        state = _State(1, 0x1 | 0x4, 0)  # ProcessPowerThrottling = 4
        return bool(k32.SetProcessInformation(k32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state)))
    except Exception:
        return False


def _is_studio_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/health", timeout=2) as response:
            return bool(json.loads(response.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def main():
    cfg = load_config()
    host = str(cfg["host"])
    if host not in LOOPBACK:
        print(f"[warn] host '{host}' in config.json is not loopback; Aksara Voice only listens on 127.0.0.1.")
        host = "127.0.0.1"
    port = int(cfg["port"])
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            print("[warn] --port needs a number; using", port)
    url = f"http://{host}:{port}/"
    no_browser = "--no-browser" in sys.argv

    # Check the port before loading models (Chatterbox takes a while to load).
    if _is_studio_running(url):
        print(f"Aksara Voice is already running at {url}")
        if not no_browser:
            webbrowser.open(url)
        return 0

    if disable_power_throttling():
        print("Windows power throttling disabled for Aksara Voice (full CPU speed).", flush=True)
    app = App()
    try:
        server = Server((host, port), make_handler(app))
    except OSError as ex:
        in_use = ex.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)) or \
            getattr(ex, "winerror", None) in (10048, 10013)
        if in_use and _is_studio_running(url):
            print(f"Aksara Voice is already running at {url}")
            if not no_browser:
                webbrowser.open(url)
            return 0
        if in_use:
            print(f"[error] Port {port} is used by another program. Close it or run: app.py --port 7790")
        else:
            print(f"[error] Cannot listen on {url}: {ex}")
        return 1
    print(f"Aksara Voice {VERSION} running at {url}", flush=True)
    print("Engines: Aksara HD (Malay) | Aksara Local (English) | Aksara Cloud (Malay + English)", flush=True)
    if not no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nAksara Voice stopping…", flush=True)
    finally:
        server.server_close()
        print("Aksara Voice stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
