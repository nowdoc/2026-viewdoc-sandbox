"""
URL-to-Viewer dispatcher for Apache Guacamole.

User flow:
    GET /?url=<file-url>
        1. classify the file by extension
        2. spawn a VNC viewer container (image: xvfb+x11vnc+xpdf|feh|vlc)
           attached to the guac_net docker network — no host port mapping.
        3. wait for the container's :5900 (resolved by container name on the
           docker network — reachable by both guacd and the dispatcher).
        4. mint a Guacamole json-auth blob describing exactly ONE VNC
           connection to <container_name>:5900, exchange it for an
           authToken via POST /api/tokens.
        5. 302 redirect to
                 http://<request-host>:<GUAC_PUBLIC_PORT>/guacamole/#/client/<id>?token=<tok>
           where <request-host> comes from the incoming Host header
           (or PUBLIC_HOST env override). We NEVER hardcode localhost —
           the prototype must work over the LAN.

Why json-auth instead of REST connection creation?
    The basic-user-mapping auth provider returns 403 on
    POST /api/session/data/default/connections (no admin rights). json-auth
    sidesteps the whole connection-management API: the dispatcher signs and
    encrypts a JSON blob that IS the connection definition, and Guacamole
    creates a single-use session bound to that connection.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import time
import urllib.parse
import uuid
from typing import Optional

import docker
import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("dispatcher")

# ---- configuration -----------------------------------------------------------

GUACAMOLE_INTERNAL_URL = os.environ.get(
    "GUACAMOLE_INTERNAL_URL", "http://guacamole:8080/guacamole"
).rstrip("/")

# json-auth shared secret. MUST equal the JSON_SECRET_KEY on the guacamole
# container. Format: 32-char hex (16-byte AES-128 key).
JSON_SECRET_KEY_HEX = os.environ.get(
    "JSON_SECRET_KEY", "4c0b569e4c0b569e4c0b569e4c0b569e"
)

# Public host/port the BROWSER will use to reach Guacamole.
#   - PUBLIC_HOST overrides everything (use when the Host header is unreliable,
#     e.g. behind a reverse proxy that doesn't preserve it).
#   - Otherwise we derive the host from the incoming request's Host header,
#     keeping the user on the same hostname/IP they typed.
# We NEVER fall back to "localhost" in the redirect path — the prototype is
# explicitly required to be reachable from other machines on the LAN.
PUBLIC_HOST = (os.environ.get("PUBLIC_HOST") or "").strip() or None
GUAC_PUBLIC_PORT = int(os.environ.get("GUAC_PUBLIC_PORT", "7080"))
GUAC_PUBLIC_PATH = os.environ.get("GUAC_PUBLIC_PATH", "/guacamole").rstrip("/") or ""

VIEWER_IMAGE = os.environ.get("VIEWER_IMAGE", "guac-viewer:latest")
VIEWER_NETWORK = os.environ.get("VIEWER_NETWORK", "guac_net")
# guacd reaches viewer containers BY CONTAINER NAME inside the docker network —
# no host port mapping for VNC. Stays internal.
VIEWER_VNC_PORT = int(os.environ.get("VIEWER_VNC_PORT", "5900"))
VNC_READY_TIMEOUT_S = float(os.environ.get("VNC_READY_TIMEOUT_S", "30"))

# The json-auth extension's "data source" name as it appears in the client
# identifier we build for the redirect URL.
JSON_DATA_SOURCE = "json"

EXT_TO_VIEWER = {
    "pdf":  ("xpdf", "pdf"),
    "png":  ("feh", "png"),
    "jpg":  ("feh", "jpg"),
    "jpeg": ("feh", "jpeg"),
    "gif":  ("feh", "gif"),
    "bmp":  ("feh", "bmp"),
    "mp3":  ("vlc", "mp3"),
    "wav":  ("vlc", "wav"),
    "ogg":  ("vlc", "ogg"),
    "mp4":  ("vlc", "mp4"),
    "webm": ("vlc", "webm"),
    "mkv":  ("vlc", "mkv"),
    "mov":  ("vlc", "mov"),
}


# ---- helpers -----------------------------------------------------------------

def classify(url: str) -> tuple[str, str]:
    """Return (viewer_bin, file_ext) for the given URL."""
    path = urllib.parse.urlparse(url).path.lower()
    if "." in path:
        ext = path.rsplit(".", 1)[-1]
        if ext in EXT_TO_VIEWER:
            return EXT_TO_VIEWER[ext]
    return ("feh", "bin")


def docker_client() -> docker.DockerClient:
    return docker.from_env()


def wait_for_tcp(host: str, port: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def spawn_viewer(url: str, viewer: str, file_ext: str) -> tuple[str, str]:
    """Run the viewer container and return (container_id, container_name).

    No host port mapping is added — guacd reaches the container by name on
    the shared docker network (`VIEWER_NETWORK`). This keeps VNC fully
    internal, so the viewer is NOT exposed on the LAN.
    """
    client = docker_client()
    short_id = secrets.token_hex(4)
    name = f"guac-viewer-{short_id}"

    log.info("spawning viewer %s viewer=%s ext=%s url=%s", name, viewer, file_ext, url)
    container = client.containers.run(
        VIEWER_IMAGE,
        name=name,
        detach=True,
        remove=True,
        environment={
            "TARGET_URL": url,
            "VIEWER": viewer,
            "FILE_EXT": file_ext,
        },
        # NOTE: no `ports=` — VNC stays on the docker network only.
        labels={"guac-viewer": "true"},
        network=VIEWER_NETWORK,
    )

    # The dispatcher is on the same docker network, so the container name
    # resolves via docker's embedded DNS. Wait for VNC to come up.
    if not wait_for_tcp(name, VIEWER_VNC_PORT, VNC_READY_TIMEOUT_S):
        raise RuntimeError(
            f"viewer {name} did not accept VNC on :{VIEWER_VNC_PORT} within "
            f"{VNC_READY_TIMEOUT_S}s"
        )

    log.info("viewer %s ready: vnc=%s:%d (docker-internal)", name, name, VIEWER_VNC_PORT)
    return container.id, name


# ---- Guacamole json-auth ----------------------------------------------------

def _json_secret_bytes() -> bytes:
    """Decode the json-auth secret. The extension expects a hex-encoded
    AES-128 key (16 bytes / 32 hex chars). We accept and pad/truncate to
    16 bytes to be tolerant of misconfiguration."""
    s = JSON_SECRET_KEY_HEX.strip()
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        raw = s.encode()
    return (raw + b"\0" * 16)[:16]


def mint_json_auth_blob(
    *, viewer_host: str, viewer_port: int, username: str, conn_name: str
) -> str:
    """Build a signed+encrypted json-auth blob describing a single VNC
    connection. Returns the base64-encoded ciphertext suitable for posting
    as the `data` form field to /api/tokens.

    Wire format (see guacamole-auth-json):
        plaintext = HMAC_SHA256(key, json_bytes) || json_bytes
        ciphertext = AES_128_CBC(key, iv=0).encrypt(PKCS7(plaintext))
        blob = base64(ciphertext)
    """
    key = _json_secret_bytes()
    payload = {
        "username": username,
        # 10-minute expiry (ms since epoch)
        "expires": int((time.time() + 600) * 1000),
        "connections": {
            conn_name: {
                "protocol": "vnc",
                "parameters": {
                    "hostname": viewer_host,
                    "port": str(viewer_port),
                    "ignore-cert": "true",
                    "color-depth": "24",
                    "cursor": "remote",
                },
            }
        },
    }
    json_bytes = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(key, json_bytes, hashlib.sha256).digest()
    plaintext = sig + json_bytes
    iv = b"\0" * 16
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(plaintext, AES.block_size))
    return base64.b64encode(ciphertext).decode()


def exchange_for_token(blob: str) -> str:
    """POST the json-auth blob to /api/tokens; return the authToken string."""
    url = f"{GUACAMOLE_INTERNAL_URL}/api/tokens"
    with httpx.Client(timeout=10) as c:
        r = c.post(url, data={"data": blob})
        if r.status_code >= 400:
            log.error("guacamole /api/tokens failed: %s %s", r.status_code, r.text)
            r.raise_for_status()
        return r.json()["authToken"]


def encoded_client_id(connection_name: str) -> str:
    """Guacamole client identifier format:
        base64(name + '\\0' + 'c' + '\\0' + dataSource)
    For the json-auth extension the data source is 'json'.
    """
    raw = f"{connection_name}\x00c\x00{JSON_DATA_SOURCE}".encode()
    return base64.b64encode(raw).decode().rstrip("=")


# ---- request -> public URL plumbing -----------------------------------------

def public_host_for(request: Request) -> str:
    """Return the hostname/IP to put in the redirect Location header.

    Precedence:
      1. PUBLIC_HOST env var (operator override)
      2. Host header from the incoming request (strip the port if present)
      3. The request's URL hostname (fallback for clients that omit Host)

    We deliberately never substitute "localhost" here — see module docstring.
    """
    if PUBLIC_HOST:
        return PUBLIC_HOST
    host_hdr = request.headers.get("host")
    if host_hdr:
        h = host_hdr.strip()
        # IPv6 literal: "[::1]:7081"
        if h.startswith("["):
            end = h.find("]")
            if end != -1:
                return h[: end + 1]
        # IPv4 / hostname with optional ":port"
        if ":" in h:
            return h.rsplit(":", 1)[0]
        return h
    return request.url.hostname or "127.0.0.1"


def build_public_url(host: str, client_b64: str, token: str) -> str:
    return (
        f"http://{host}:{GUAC_PUBLIC_PORT}{GUAC_PUBLIC_PATH}/#/client/"
        f"{client_b64}?token={token}"
    )


# ---- app ---------------------------------------------------------------------

app = FastAPI(title="guac-dispatcher")


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, url: Optional[str] = Query(default=None)):
    if not url:
        host = public_host_for(request)
        return HTMLResponse(
            f"""<!doctype html><html><body style="font-family:sans-serif;max-width:680px;margin:3em auto">
            <h1>Guacamole URL Dispatcher</h1>
            <p>Reached via host <code>{host}</code>. Guacamole is at
            <a href="http://{host}:{GUAC_PUBLIC_PORT}{GUAC_PUBLIC_PATH}/">
            http://{host}:{GUAC_PUBLIC_PORT}{GUAC_PUBLIC_PATH}/</a>.</p>
            <p>Append <code>?url=&lt;file-url&gt;</code> to the URL. Examples:</p>
            <ul>
              <li><a href="?url=https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png">PNG</a></li>
              <li><a href="?url=https://www.africau.edu/images/default/sample.pdf">PDF</a></li>
              <li><a href="?url=https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3">MP3</a></li>
              <li><a href="?url=https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4">MP4</a></li>
            </ul>
            </body></html>""",
            status_code=200,
        )

    viewer, file_ext = classify(url)
    log.info("dispatch url=%s viewer=%s ext=%s", url, viewer, file_ext)

    try:
        container_id, container_name = spawn_viewer(url, viewer, file_ext)
    except Exception as e:
        log.exception("spawn failed")
        raise HTTPException(500, f"viewer spawn failed: {e}")

    conn_name = f"viewer-{container_id[:8]}"
    username = f"u-{uuid.uuid4().hex[:8]}"

    try:
        blob = mint_json_auth_blob(
            viewer_host=container_name,
            viewer_port=VIEWER_VNC_PORT,
            username=username,
            conn_name=conn_name,
        )
        token = exchange_for_token(blob)
    except Exception as e:
        log.exception("json-auth exchange failed")
        raise HTTPException(502, f"guacamole token exchange failed: {e}")

    cid_b64 = encoded_client_id(conn_name)
    host = public_host_for(request)
    redirect_to = build_public_url(host, cid_b64, token)
    log.info(
        "redirect -> %s (conn=%s, vnc=%s:%d, host_hdr=%r, public_host=%s)",
        redirect_to, conn_name, container_name, VIEWER_VNC_PORT,
        request.headers.get("host"), host,
    )
    return RedirectResponse(redirect_to, status_code=302)


@app.get("/cleanup")
def cleanup():
    client = docker_client()
    removed = []
    for c in client.containers.list(filters={"label": "guac-viewer=true"}):
        try:
            c.kill()
            removed.append(c.name)
        except Exception as e:
            log.warning("cleanup %s: %s", c.name, e)
    return JSONResponse({"stopped": removed})
