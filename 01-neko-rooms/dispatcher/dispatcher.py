"""
neko-rooms dispatcher
---------------------

Tiny HTTP service. One endpoint:

    GET /?url=<target-url>

Behaviour:
  1. Classifies the URL by file extension.
       image / pdf / html / unknown -> Firefox (custom LAUNCH_URL image)
       audio / video                -> VLC (stock image, VLC_MEDIA env)
  2. Calls the neko-rooms admin API to spawn a fresh room with the right
     image and the URL injected as an env var.
  3. 302-redirects the user to the room's URL.

The 302 Location host is built from the *incoming request's Host header*
(falling back to PUBLIC_HOST env var if set), not a hardcoded "localhost".
That way the prototype works when accessed from another machine on the
network: a request to http://<your-host>:8081/?url=... redirects back to
http://<your-host>:8080/room/<name>/.

Stdlib-only (no pip).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Config (env-driven) ---------------------------------------------------

NEKO_ROOMS_API = os.environ.get("NEKO_ROOMS_API", "http://neko-rooms:8080")
FIREFOX_IMAGE = os.environ.get(
    "FIREFOX_IMAGE", "kasm2/neko-firefox-launch:latest"
)
VLC_IMAGE = os.environ.get(
    "VLC_IMAGE", "ghcr.io/m1k1o/neko/vlc:latest"
)
PUBLIC_HOST_OVERRIDE = os.environ.get("PUBLIC_HOST", "").strip()
PATH_PREFIX = os.environ.get("NEKO_ROOMS_PATH_PREFIX", "/room/")
LISTEN_HOST = "0.0.0.0"  # all interfaces — required for LAN access
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8081"))


# --- Classification --------------------------------------------------------

# Extension -> (kind, image_role). image_role decides which neko app spawns.
EXT_TABLE: dict[str, tuple[str, str]] = {
    # images and html-ish content -> firefox
    "png": ("image", "firefox"),
    "jpg": ("image", "firefox"),
    "jpeg": ("image", "firefox"),
    "gif": ("image", "firefox"),
    "webp": ("image", "firefox"),
    "svg": ("image", "firefox"),
    "bmp": ("image", "firefox"),
    "ico": ("image", "firefox"),
    "pdf": ("pdf", "firefox"),
    "html": ("html", "firefox"),
    "htm": ("html", "firefox"),
    # audio/video -> vlc
    "mp3": ("audio", "vlc"),
    "wav": ("audio", "vlc"),
    "ogg": ("audio", "vlc"),
    "flac": ("audio", "vlc"),
    "m4a": ("audio", "vlc"),
    "aac": ("audio", "vlc"),
    "mp4": ("video", "vlc"),
    "mkv": ("video", "vlc"),
    "webm": ("video", "vlc"),
    "mov": ("video", "vlc"),
    "avi": ("video", "vlc"),
}


def classify(target_url: str) -> tuple[str, str]:
    """Return (kind, image_role). Unknown extensions fall back to html/firefox."""
    path = urllib.parse.urlparse(target_url).path.lower()
    # take extension off the last path segment only
    last = path.rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1] if "." in last else ""
    return EXT_TABLE.get(ext, ("html", "firefox"))


# --- neko-rooms API call ---------------------------------------------------

AUTO_USER_PASS = "guest"
AUTO_ADMIN_PASS = "admin"


def build_settings(target_url: str, kind: str, role: str, room_name: str) -> dict:
    """Build the RoomSettings JSON for neko-rooms POST /api/rooms."""
    if role == "vlc":
        return {
            "name": room_name,
            "neko_image": VLC_IMAGE,
            "max_connections": 0,
            "user_pass": AUTO_USER_PASS,
            "admin_pass": AUTO_ADMIN_PASS,
            "implicit_control": True,
            "envs": {"VLC_MEDIA": target_url},
        }

    return {
        "name": room_name,
        "neko_image": FIREFOX_IMAGE,
        "max_connections": 0,
        "user_pass": AUTO_USER_PASS,
        "admin_pass": AUTO_ADMIN_PASS,
        "implicit_control": True,
        "envs": {"LAUNCH_URL": target_url},
    }


def create_room(settings: dict) -> dict:
    """POST /api/rooms?start=true. Return the RoomEntry response body."""
    data = json.dumps(settings).encode("utf-8")
    req = urllib.request.Request(
        f"{NEKO_ROOMS_API}/api/rooms?start=true",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


# --- Redirect host handling ------------------------------------------------

def host_from_request(handler: BaseHTTPRequestHandler) -> str:
    """
    Pick the hostname to use in the redirect Location.

    Priority:
      1. PUBLIC_HOST env var (admin override).
      2. Hostname part of the incoming Host header (port stripped).
      3. Last-resort fall-back to our own bind address.

    We strip the port because the room runs on neko-rooms' port (8080),
    not the dispatcher's port (8081). The port comes from the room URL.
    """
    if PUBLIC_HOST_OVERRIDE:
        return PUBLIC_HOST_OVERRIDE.split(":", 1)[0]
    host_header = handler.headers.get("Host", "").strip()
    if host_header:
        return host_header.split(":", 1)[0]
    return LISTEN_HOST


def rewrite_location(room: dict, request_hostname: str) -> str:
    """
    Build the final redirect URL.

    neko-rooms returns a `url` field built from NEKO_ROOMS_INSTANCE_URL.
    We keep its scheme, path and port — but swap the hostname for the host
    the client actually used, so a request from a LAN IP redirects back to
    that same LAN IP, never to 127.0.0.1.
    """
    room_url = room.get("url") or ""
    if not room_url:
        # Synthesise (rare).
        return f"http://{request_hostname}:8080{PATH_PREFIX}{room.get('name', '')}/"

    parsed = urllib.parse.urlparse(room_url)
    port = f":{parsed.port}" if parsed.port else ""
    # Append usr+pwd query params so neko's client auto-submits the login
    # form — no password prompt for the user.
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    existing.extend([("usr", "guest"), ("pwd", AUTO_USER_PASS)])
    rebuilt = parsed._replace(
        netloc=f"{request_hostname}{port}",
        query=urllib.parse.urlencode(existing),
    )
    return urllib.parse.urlunparse(rebuilt)


# --- HTTP handler ----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "neko-rooms-dispatcher/0.2"

    def _send_text(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/healthz":
            self._send_text(200, "ok\n")
            return

        if parsed.path == "/classify":
            qs = urllib.parse.parse_qs(parsed.query)
            url = (qs.get("url") or [""])[0]
            if not url:
                self._send_text(400, "missing url\n")
                return
            kind, role = classify(url)
            self._send_json(200, {"url": url, "kind": kind, "image_role": role})
            return

        if parsed.path != "/":
            self._send_text(404, "not found\n")
            return

        qs = urllib.parse.parse_qs(parsed.query)
        urls = qs.get("url")
        if not urls or not urls[0]:
            self._send_text(
                200,
                "kasm2 url-to-viewer dispatcher\n"
                "usage: GET /?url=<target-url>\n"
                "       creates an ephemeral neko room and 302s into it.\n",
            )
            return

        target_url = urls[0]
        scheme = urllib.parse.urlparse(target_url).scheme
        if scheme not in ("http", "https"):
            self._send_text(400, f"unsupported url scheme: {scheme!r}\n")
            return

        kind, role = classify(target_url)
        room_name = "v-" + secrets.token_hex(4)
        settings = build_settings(target_url, kind, role, room_name)

        sys.stderr.write(
            f"[dispatcher] dispatch url={target_url!r} kind={kind} role={role} "
            f"image={settings['neko_image']} room={room_name}\n"
        )

        try:
            room = create_room(settings)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            sys.stderr.write(f"[dispatcher] neko-rooms {exc.code}: {body}\n")
            self._send_text(502, f"neko-rooms error {exc.code}: {body}\n")
            return
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[dispatcher] neko-rooms failed: {exc}\n")
            self._send_text(502, f"failed to create room: {exc}\n")
            return

        request_hostname = host_from_request(self)
        location = rewrite_location(room, request_hostname)

        sys.stderr.write(
            f"[dispatcher] created room id={room.get('id')} name={room.get('name')} "
            f"-> 302 {location}\n"
        )

        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[http] " + (fmt % args) + "\n")


def main() -> None:
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    sys.stderr.write(
        f"[dispatcher] listening on {LISTEN_HOST}:{LISTEN_PORT}, "
        f"neko-rooms api={NEKO_ROOMS_API}, "
        f"firefox_image={FIREFOX_IMAGE}, vlc_image={VLC_IMAGE}, "
        f"public_host_override={PUBLIC_HOST_OVERRIDE!r}\n"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
