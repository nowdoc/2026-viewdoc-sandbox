#!/usr/bin/env python3
"""Tiny dispatcher for the xpra-viewer prototype.

Flow:
  GET /?url=<asset_url>[&kind=pdf|image|video|audio]
    1. Pick a viewer kind (from query or by guessing from URL extension).
    2. Pick a free port from the dispatcher's reserved range (default
       9082-9099) by asking the docker engine which ports are already mapped.
    3. `docker run -d -p <port>:14500 ... xpra-viewer:latest`. With no bind-IP
       prefix, docker binds the host port on 0.0.0.0 and ::, so it is reachable
       from any interface on the LAN.
    4. Wait until xpra returns 200 on / (so we don't redirect too early).
    5. 302 redirect the browser to http://<request-host>:<host_port>/
       where <request-host> is taken from the incoming Host header (so the
       redirect works from any machine on the LAN, not just localhost).
    6. A background janitor docker-rm's containers older than IDLE_TTL.

  GET /healthz
    -> 200 ok

The dispatcher is intentionally stateless across requests apart from the
janitor thread that GC's idle containers.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Config (env)
# ---------------------------------------------------------------------------
DISPATCHER_PORT = int(os.environ.get("DISPATCHER_PORT", "9081"))
VIEWER_IMAGE = os.environ.get("VIEWER_IMAGE", "xpra-viewer:latest")
# Container-internal port xpra binds to. Must match EXPOSE in Dockerfile.viewer.
XPRA_CONTAINER_PORT = int(os.environ.get("XPRA_CONTAINER_PORT", "14500"))
# How long an idle viewer container is allowed to live (seconds).
IDLE_TTL = int(os.environ.get("IDLE_TTL", "900"))
# Docker network the viewer containers should attach to (optional).
VIEWER_NETWORK = os.environ.get("VIEWER_NETWORK", "")
# If set, override the host part of the 302 Location (useful when the dispatcher
# is behind a proxy that rewrites Host, or when Host header is unreliable).
# When unset (default) we derive the host from the request's Host header.
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()
# Ephemeral host-port range we hand out for viewer containers. We pick a free
# port from this range ourselves (rather than letting docker pick from the
# kernel ephemeral range) to keep prototype ports inside a small, predictable
# window — easier to firewall, easier to remember, doesn't collide with the
# other prototypes in this monorepo (3000, 8080-8089, 32768-32770).
VIEWER_PORT_RANGE_START = int(os.environ.get("VIEWER_PORT_RANGE_START", "9082"))
VIEWER_PORT_RANGE_END = int(os.environ.get("VIEWER_PORT_RANGE_END", "9099"))

log = logging.getLogger("dispatcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# ---------------------------------------------------------------------------
# Container lifecycle tracking (for the janitor)
# ---------------------------------------------------------------------------
# Map container_id -> last_seen_epoch. Updated when we spawn and (in a fuller
# impl) when the viewer page is hit. For the prototype we just GC by age.
_tracked: dict[str, float] = {}
_tracked_lock = threading.Lock()


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a docker CLI command and return CompletedProcess.

    We shell out to the docker CLI rather than using docker-py to keep the
    dispatcher dependency-free (stdlib only).
    """
    cmd = ["docker", *args]
    log.debug("exec: %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
    )


def guess_kind(url: str) -> str:
    """Best-effort guess of the viewer kind based on URL extension."""
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")):
        return "image"
    if path.endswith((".mp4", ".mkv", ".webm", ".mov", ".avi")):
        return "video"
    if path.endswith((".mp3", ".wav", ".flac", ".ogg", ".m4a")):
        return "audio"
    return "pdf"  # default — most "show me this document" cases


_port_lock = threading.Lock()


def _pick_free_port() -> int:
    """Pick a port from [VIEWER_PORT_RANGE_START, VIEWER_PORT_RANGE_END] that is
    not currently published by any docker container.

    We don't drop --cap-drop or use any clever IPC; we just ask docker which
    host ports are already mapped and skip them. Race window between this read
    and `docker run` is small enough for a single-dispatcher prototype.
    """
    try:
        proc = _docker("ps", "--format", "{{.Ports}}", "--no-trunc", check=False)
        in_use: set[int] = set()
        for line in proc.stdout.splitlines():
            # Lines look like: "0.0.0.0:9082->14500/tcp, :::9082->14500/tcp"
            for chunk in line.split(","):
                chunk = chunk.strip()
                if "->" not in chunk:
                    continue
                left = chunk.split("->", 1)[0]
                # left = "0.0.0.0:9082" or ":::9082"
                if ":" in left:
                    try:
                        in_use.add(int(left.rsplit(":", 1)[1]))
                    except ValueError:
                        pass
        for p in range(VIEWER_PORT_RANGE_START, VIEWER_PORT_RANGE_END + 1):
            if p not in in_use:
                return p
    except Exception as e:  # pragma: no cover
        log.warning("port discovery via docker ps failed: %s", e)
    raise RuntimeError(
        f"no free port in {VIEWER_PORT_RANGE_START}-{VIEWER_PORT_RANGE_END}"
    )


def spawn_viewer(view_url: str, kind: str) -> tuple[str, int]:
    """Start a viewer container and return (container_id, host_port).

    We pin the host-side port to a value we picked from our small range, so
    operators only need to firewall 9082-9099 instead of the whole kernel
    ephemeral range. Because we do NOT prefix the host part with an IP, docker
    binds on 0.0.0.0 (and ::), so the port is reachable from any interface.
    """
    name = f"xpra-viewer-{uuid.uuid4().hex[:10]}"
    with _port_lock:
        host_port = _pick_free_port()
    args = [
        "run", "-d", "--rm",
        "--name", name,
        "-p", f"{host_port}:{XPRA_CONTAINER_PORT}",  # explicit host port, all interfaces
        "-e", f"TARGET_URL={view_url}",
        "-e", f"VIEWER={_kind_to_viewer(kind)}",
        "--memory=1g", "--cpus=1.0",
    ]
    if VIEWER_NETWORK:
        args += ["--network", VIEWER_NETWORK]
    args.append(VIEWER_IMAGE)

    proc = _docker(*args)
    container_id = proc.stdout.strip()
    if not container_id:
        raise RuntimeError(f"docker run produced no id: {proc.stderr!r}")
    log.info(
        "spawned viewer container %s (%s) for %s on host port %d",
        name, container_id[:12], view_url, host_port,
    )

    # Poll readiness so the 302 doesn't redirect the browser before xpra binds.
    _wait_ready(host_port, timeout=15.0)
    with _tracked_lock:
        _tracked[container_id] = time.time()
    return container_id, host_port


def _wait_ready(host_port: int, timeout: float = 15.0) -> None:
    """Poll the xpra HTML5 endpoint until it returns 200 or timeout expires.

    Uses a raw socket connect + a tiny HTTP GET / so we have no extra deps.
    The dispatcher container can reach the host-side port via the special
    host.docker.internal alias (we add an extra_hosts mapping in compose).
    """
    import socket
    deadline = time.time() + timeout
    host = "host.docker.internal"
    while time.time() < deadline:
        try:
            with socket.create_connection((host, host_port), timeout=1.0) as s:
                s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
                data = s.recv(64)
                if data.startswith(b"HTTP/") and b" 200" in data[:20]:
                    return
        except Exception:
            pass
        time.sleep(0.3)
    log.warning("xpra on port %d did not return 200 within %.1fs", host_port, timeout)


def _kind_to_viewer(kind: str) -> str:
    return {
        "pdf": "xpdf",
        "image": "feh",
        "video": "mpv",
        "audio": "mpv",
    }.get(kind, "xpdf")


# NOTE: _read_host_port was removed: we now pin the host port at spawn time
# via _pick_free_port(), so the dispatcher already knows the port and no
# docker-inspect roundtrip is needed.


# ---------------------------------------------------------------------------
# Janitor
# ---------------------------------------------------------------------------
def _janitor_loop() -> None:
    while True:
        time.sleep(30)
        now = time.time()
        to_kill: list[str] = []
        with _tracked_lock:
            for cid, ts in list(_tracked.items()):
                if now - ts > IDLE_TTL:
                    to_kill.append(cid)
                    _tracked.pop(cid, None)
        for cid in to_kill:
            try:
                _docker("rm", "-f", cid, check=False)
                log.info("janitor reaped %s", cid[:12])
            except Exception as e:
                log.warning("janitor failed to reap %s: %s", cid[:12], e)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "xpra-dispatcher/0.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib override
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
        # HEAD on /healthz returns headers only. HEAD on /?url= would otherwise
        # spawn a viewer container just to discard the body — wasteful for
        # health probes. Return 405 to discourage that; verification scripts
        # should use `curl -i` (GET with response headers shown) instead.
        if self.path.startswith("/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.end_headers()

    def _redirect_host(self) -> str:
        """Determine the host part to use in the 302 Location header.

        Priority:
          1. PUBLIC_HOST env override (operator-controlled, useful behind a
             reverse proxy that strips/rewrites Host).
          2. The Host header from the incoming request, with any :port stripped
             (we'll attach the docker-assigned port). This lets a LAN user
             keep getting redirected back to the IP/hostname they connected
             on, instead of "localhost".
          3. Fall back to "localhost" only as a last resort.
        """
        if PUBLIC_HOST:
            return PUBLIC_HOST
        host_hdr = self.headers.get("Host", "").strip()
        if host_hdr:
            # Strip the port; Host is "ip:port" or "[ipv6]:port" or "name:port".
            if host_hdr.startswith("["):
                # IPv6 literal: "[::1]:9081" -> "[::1]"
                end = host_hdr.find("]")
                if end != -1:
                    return host_hdr[: end + 1]
                return host_hdr
            if ":" in host_hdr:
                return host_hdr.rsplit(":", 1)[0]
            return host_hdr
        return "localhost"

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            body = json.dumps({
                "ok": True,
                "viewer_image": VIEWER_IMAGE,
                "xpra_container_port": XPRA_CONTAINER_PORT,
                "viewer_port_range": f"{VIEWER_PORT_RANGE_START}-{VIEWER_PORT_RANGE_END}",
                "public_host_override": PUBLIC_HOST or None,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path != "/":
            self.send_error(404, "not found")
            return

        qs = parse_qs(parsed.query)
        urls = qs.get("url") or qs.get("u")
        if not urls:
            body = (
                b"<html><body><h1>xpra dispatcher</h1>"
                b"<p>Usage: <code>/?url=&lt;asset_url&gt;[&amp;kind=pdf|image|video|audio]</code></p>"
                b"</body></html>"
            )
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        url = urls[0]
        kind = (qs.get("kind", [""])[0] or guess_kind(url)).lower()

        try:
            container_id, host_port = spawn_viewer(url, kind)
        except subprocess.CalledProcessError as e:
            log.exception("docker run failed")
            self.send_error(502, f"docker run failed: {e.stderr.strip()[:200]}")
            return
        except Exception as e:
            log.exception("spawn failed")
            self.send_error(500, f"spawn failed: {e}")
            return

        host = self._redirect_host()
        # xpra-html5's index page lives at "/" of the bind-tcp+html=on port.
        location = f"http://{host}:{host_port}/"
        log.info("redirecting -> %s (container %s)", location, container_id[:12])
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Viewer-Container", container_id[:12])
        self.send_header("X-Viewer-Kind", kind)
        self.end_headers()


def main() -> None:
    # Bind on 0.0.0.0 so the dispatcher itself is reachable from the LAN.
    threading.Thread(target=_janitor_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", DISPATCHER_PORT), Handler)
    log.info(
        "dispatcher listening on 0.0.0.0:%d (viewer image=%s, ttl=%ds, public_host=%s)",
        DISPATCHER_PORT, VIEWER_IMAGE, IDLE_TTL, PUBLIC_HOST or "<from Host header>",
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
