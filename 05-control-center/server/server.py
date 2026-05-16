#!/usr/bin/env python3
"""Control-center HTTP server (port 5081).

Self-contained iframe-bridge. Owns a fixed pool of pre-warmed kasm/ubuntu
containers (default size 3, configurable). Each `/api/session` request leases
one slot round-robin. URL changes from the parent page are hot-patched into
the leased container's /tmp/kasm_query.{json,env} via `docker exec`.

Why a pool, not dynamic spawn?
  - Kasm cold-start is 10-25s. Pool means /api/session returns instantly.
  - Capped resource use, predictable host ports.
  - Simpler error model (no port-pick failures at request time).

stdlib-only. We shell out to the docker CLI (mounted via /var/run/docker.sock)
to spawn the pool at startup and `exec` into slots at request time.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import secrets
import select
import shlex
import signal
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse

# ---------------------------------------------------------------------------
# Config (env)
# ---------------------------------------------------------------------------
CONTROL_CENTER_PORT = int(os.environ.get("CONTROL_CENTER_PORT", "5081"))

# Container-internal KasmVNC port. kasmweb/* images all expose 6901.
KASM_CONTAINER_PORT = int(os.environ.get("KASM_CONTAINER_PORT", "6901"))

# Pool size + deterministic host port window. Slot i gets host port
# VIEWER_PORT_RANGE_START + i. Stay clear of other prototypes on this host.
VIEWER_POOL_SIZE = int(os.environ.get("VIEWER_POOL_SIZE", "3"))
VIEWER_PORT_RANGE_START = int(os.environ.get("VIEWER_PORT_RANGE_START", "5082"))

# How long we wait for each slot to start serving HTTP before declaring boot
# failure. Kasm cold-start is 10-25s; allow plenty of slack.
READY_TIMEOUT_S = float(os.environ.get("READY_TIMEOUT_S", "90"))

# Container-side TLS opt-out for KasmVNC.
KASM_SVC_HTTPS_VALUE = os.environ.get("KASM_SVC_HTTPS_VALUE", "disabled")

# Per-container resource caps.
VIEWER_MEMORY = os.environ.get("VIEWER_MEMORY", "2g")
VIEWER_CPUS = os.environ.get("VIEWER_CPUS", "2.0")

# NoVNC viewer URL params (KasmVNC's fork). These are written into the
# iframe `src` so they apply on first connect — KasmVNC's UI reads them
# from the URL and overrides its stored settings.
#   quality          0..9, JPEG/WebP quality (default 6 upstream; 9 = lossless-ish)
#   compression      0..9, zlib level on the wire (lower = faster CPU, more bytes)
#   dynamic_quality_min/max  per-tile lower/upper bounds for the adaptive encoder
#   prefer_local_cursor      render cursor in the browser (cheaper than streaming it)
#   clipboard_*              KasmVNC clipboard flags (seamless = bidi auto-sync)
#   resize                   `remote` asks the server to match the iframe size
# Defaults chosen for "high quality" out of the box; override via env.
VIEWER_QUALITY              = os.environ.get("VIEWER_QUALITY", "9")
VIEWER_COMPRESSION          = os.environ.get("VIEWER_COMPRESSION", "2")
VIEWER_DYNAMIC_QUALITY_MIN  = os.environ.get("VIEWER_DYNAMIC_QUALITY_MIN", "7")
VIEWER_DYNAMIC_QUALITY_MAX  = os.environ.get("VIEWER_DYNAMIC_QUALITY_MAX", "9")
VIEWER_PREFER_LOCAL_CURSOR  = os.environ.get("VIEWER_PREFER_LOCAL_CURSOR", "1")
VIEWER_CLIPBOARD_UP         = os.environ.get("VIEWER_CLIPBOARD_UP", "1")
VIEWER_CLIPBOARD_DOWN       = os.environ.get("VIEWER_CLIPBOARD_DOWN", "1")
VIEWER_CLIPBOARD_SEAMLESS   = os.environ.get("VIEWER_CLIPBOARD_SEAMLESS", "1")
VIEWER_RESIZE               = os.environ.get("VIEWER_RESIZE", "remote")

# Optional docker network for pool containers. Empty -> default bridge.
VIEWER_NETWORK = os.environ.get("VIEWER_NETWORK", "")

# Pool containers run native to the host arch. The Dockerfile no longer
# pins --platform, so `docker build` and `docker run` pick up the host's
# arch automatically (arm64 on aarch64, amd64 on x86_64). On arm64, the
# Kasm-installed proprietary apps (Chrome / Slack / Zoom / Signal /
# OnlyOffice) are missing because upstream's install scripts gate them on
# x86_64. To run those on arm64, host this stack on an amd64 box or pin
# the image to amd64 (will incur QEMU overhead).

# Override the Host part of the iframe URL we hand the page. Empty = derive
# from the user's incoming Host header.
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()

STATIC_DIR = Path(os.environ.get("STATIC_DIR", "/app/static"))

# Dual-stack TLS. The control-center listens on a single port and sniffs the
# first byte: 0x16 -> TLS handshake (wrap with SSL), otherwise plain HTTP.
# Same trick KasmVNC uses on its 6901 port, which is why
# https://<host>:<slot-port>/ works in the browser today. Clipboard requires
# a secure context end-to-end: when the parent page is loaded over HTTPS, the
# iframe URL is built with `https://` so the chain stays secure. Cert is
# self-signed; user must accept it once per port the first time.
TLS_ENABLED  = os.environ.get("TLS_ENABLED", "1").strip() not in ("0", "false", "no", "")
TLS_CERT_DIR = Path(os.environ.get("TLS_CERT_DIR", "/app/state"))
TLS_CERT     = TLS_CERT_DIR / "control-center.crt"
TLS_KEY      = TLS_CERT_DIR / "control-center.key"
# Subject Alt Names baked into the auto-generated self-signed cert.
# Default is intentionally minimal so the prototype carries no
# environment-specific names. Set TLS_CERT_SAN in .env to add the LAN /
# mDNS / wildcard hostnames you'll actually be hitting, e.g.:
#   TLS_CERT_SAN=DNS:localhost,DNS:viewer.lan,DNS:*.viewer.lan,IP:127.0.0.1,IP:::1
TLS_CERT_SAN = os.environ.get("TLS_CERT_SAN", "").strip() or \
    "DNS:localhost,IP:127.0.0.1,IP:::1"

# Pool-instance label so multiple control-centers on one host can coexist if
# given different instance names.
POOL_INSTANCE = os.environ.get("POOL_INSTANCE", "default").strip() or "default"

# Default kasm desktop image for the pool. Built by images/Dockerfile.ubuntu.
POOL_IMAGE = os.environ.get("POOL_IMAGE", "kasm2-cc/ubuntu:latest")

log = logging.getLogger("control-center")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


_RESERVED_QUERY_KEYS = {"desktop", "url", "u", "slot"}
_MAX_PASSTHROUGH_KEYS = 32
_MAX_PASSTHROUGH_VALUE_LEN = 4096
_PASSTHROUGH_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def build_passthrough(params: dict[str, str]) -> dict[str, str]:
    """Sanitise + cap a user-supplied params dict.

    Reserved keys (desktop/url) are dropped from the passthrough — they have
    spawn-time meaning, not param meaning. We DO honour an incoming `url`
    field at the spawn-API level by remapping it to `open_url` (see
    _handle_spawn) so the in-container hook can react.
    """
    out: dict[str, str] = {}
    for k, v in params.items():
        if k in _RESERVED_QUERY_KEYS:
            continue
        if not isinstance(v, str):
            v = str(v)
        if not _PASSTHROUGH_KEY_RE.match(k):
            log.debug("skip unsafe key %r", k)
            continue
        if len(out) >= _MAX_PASSTHROUGH_KEYS:
            break
        if len(v) > _MAX_PASSTHROUGH_VALUE_LEN:
            v = v[:_MAX_PASSTHROUGH_VALUE_LEN]
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------
def _docker(*args: str, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", *args]
    log.debug("exec: %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, check=check, capture_output=True, input=stdin)


def _container_exists(container_id: str) -> bool:
    proc = _docker("inspect", "--format", "{{.Id}}", container_id, check=False)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _wait_ready(host_port: int, timeout: float) -> bool:
    """Poll host.docker.internal:<port>/ until it serves HTTP."""
    deadline = time.time() + timeout
    host = "host.docker.internal"
    last_err: str | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, host_port), timeout=2.0) as s:
                s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
                data = s.recv(64)
                if data.startswith(b"HTTP/") and (b" 20" in data[:20] or b" 30" in data[:20]):
                    return True
                last_err = data[:40].decode(errors="replace")
        except Exception as e:
            last_err = str(e)
        time.sleep(0.5)
    log.warning("port %d not ready within %.1fs (last=%s)", host_port, timeout, last_err)
    return False


# ---------------------------------------------------------------------------
# Slot pool
# ---------------------------------------------------------------------------
class Slot:
    __slots__ = ("idx", "container_id", "container_name", "host_port", "vnc_pw", "image")

    def __init__(self, idx: int, container_id: str, container_name: str,
                 host_port: int, vnc_pw: str, image: str) -> None:
        self.idx = idx
        self.container_id = container_id
        self.container_name = container_name
        self.host_port = host_port
        self.vnc_pw = vnc_pw
        self.image = image


_slots: list[Slot] = []
_slot_lock = threading.Lock()
_next_slot_idx = 0
_pool_ready = threading.Event()


def _label_filter() -> list[str]:
    return [
        "--filter", "label=control-center=05",
        "--filter", f"label=control-center.instance={POOL_INSTANCE}",
    ]


def _cleanup_prior_pool() -> None:
    """Remove containers labeled as ours from a previous run.

    We own the (control-center=05, instance=<X>) label pair — if we find
    matching containers at startup they're orphans from a prior run.
    """
    proc = _docker("ps", "-aq", *_label_filter(), check=False)
    ids = [s for s in proc.stdout.decode().split() if s]
    if not ids:
        return
    log.info("cleaning %d prior pool container(s): %s", len(ids), ids)
    _docker("rm", "-f", *ids, check=False)


def _spawn_slot(idx: int, image: str) -> Slot:
    name = f"kasm-cc-{POOL_INSTANCE}-{idx}"
    host_port = VIEWER_PORT_RANGE_START + idx
    vnc_pw = secrets.token_hex(8)

    args: list[str] = [
        "run", "-d", "--rm",
        "--name", name,
        "--label", "control-center=05",
        "--label", f"control-center.instance={POOL_INSTANCE}",
        "--label", f"control-center.slot={idx}",
        "-p", f"{host_port}:{KASM_CONTAINER_PORT}",  # 0.0.0.0
        "-e", f"VNC_PW={vnc_pw}",
        "-e", f"KASM_SVC_HTTPS={KASM_SVC_HTTPS_VALUE}",
        "-e", "KASM_SVC_UPLOADS=disabled",
        "-e", "KASM_SVC_DOWNLOADS=disabled",
        # Clipboard is on by default in kasmweb images (KASM_SVC_SEND_CUT_TEXT
        # / KASM_SVC_ACCEPT_CUT_TEXT default to empty -> no `-DisableXCutText`
        # flag). Setting them explicitly to empty keeps that behaviour and
        # documents intent if a future kasmweb base flips the default.
        "-e", "KASM_SVC_SEND_CUT_TEXT=",
        "-e", "KASM_SVC_ACCEPT_CUT_TEXT=",
        "--shm-size=512m",
        f"--memory={VIEWER_MEMORY}",
        f"--cpus={VIEWER_CPUS}",
    ]
    if VIEWER_NETWORK:
        args += ["--network", VIEWER_NETWORK]
    args.append(image)

    proc = _docker(*args)
    container_id = proc.stdout.decode().strip()
    if not container_id:
        raise RuntimeError(f"docker run produced no id: {proc.stderr.decode(errors='replace')!r}")
    log.info("slot %d spawned: %s (%s) on host port %d",
             idx, name, container_id[:12], host_port)
    return Slot(idx, container_id, name, host_port, vnc_pw, image)


def _init_pool() -> None:
    """Spawn the pool, wait for each slot to serve HTTP, store the slots."""
    image = POOL_IMAGE
    log.info("initialising pool: size=%d image=%s ports=%d..%d instance=%s",
             VIEWER_POOL_SIZE, image,
             VIEWER_PORT_RANGE_START,
             VIEWER_PORT_RANGE_START + VIEWER_POOL_SIZE - 1,
             POOL_INSTANCE)
    _cleanup_prior_pool()
    new_slots: list[Slot] = []
    # Fire all `docker run`s up front so kasm cold-starts overlap, then poll
    # readiness for each (also concurrent — boot finishes around the same
    # time across slots).
    for i in range(VIEWER_POOL_SIZE):
        new_slots.append(_spawn_slot(i, image))

    threads: list[threading.Thread] = []
    results: dict[int, bool] = {}
    def _poll(slot: Slot) -> None:
        results[slot.idx] = _wait_ready(slot.host_port, READY_TIMEOUT_S)
    for s in new_slots:
        t = threading.Thread(target=_poll, args=(s,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    for s in new_slots:
        if not results.get(s.idx):
            log.warning("slot %d (%s) failed readiness; serving anyway",
                        s.idx, s.container_name)

    _slots.clear()
    _slots.extend(new_slots)
    _pool_ready.set()
    log.info("pool ready: %s", [(s.idx, s.host_port) for s in _slots])


def _shutdown_pool() -> None:
    if not _slots:
        return
    ids = [s.container_id for s in _slots]
    log.info("removing pool containers: %s", [s.container_name for s in _slots])
    _docker("rm", "-f", *ids, check=False)
    _slots.clear()


def _lease_slot(idx: int | None = None) -> Slot:
    """Pick a slot by explicit index (0-based) or fall back to round-robin.

    v1: no idle/busy tracking, no per-tab affinity. Caller is trusted to
    supply a valid index — out-of-range raises.
    """
    global _next_slot_idx
    with _slot_lock:
        if not _slots:
            raise RuntimeError("pool not initialised")
        if idx is None:
            slot = _slots[_next_slot_idx % len(_slots)]
            _next_slot_idx += 1
            return slot
        if idx < 0 or idx >= len(_slots):
            raise ValueError(f"slot {idx} out of range (pool size {len(_slots)})")
        return _slots[idx]


def _find_slot(container_id: str) -> Optional[Slot]:
    for s in _slots:
        if s.container_id == container_id or s.container_id.startswith(container_id):
            return s
    return None


# ---------------------------------------------------------------------------
# /tmp/kasm_query.{json,env} writer
# ---------------------------------------------------------------------------
_write_lock = threading.Lock()


def write_query_to_container(container_id: str, params: dict[str, str]) -> None:
    """Atomically rewrite /tmp/kasm_query.{json,env} inside `container_id`.

    Prefers the in-container helper `/usr/local/bin/kasm-write-query` (baked
    into kasm2-cc/ubuntu). Falls back to inline shell+python if absent.
    """
    clean = build_passthrough(params)
    raw = urlencode(list(clean.items()))
    json_payload = json.dumps({"params": clean, "raw": raw})

    with _write_lock:
        helper_check = _docker(
            "exec", container_id, "test", "-x", "/usr/local/bin/kasm-write-query",
            check=False,
        )
        if helper_check.returncode == 0:
            proc = _docker(
                "exec", "-i", container_id,
                "/usr/local/bin/kasm-write-query",
                stdin=json_payload.encode(),
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"kasm-write-query failed (rc={proc.returncode}): "
                    f"{proc.stderr.decode(errors='replace')[:200]}"
                )
            return

        # Fallback. Same shell-quoting as kasm-write-query so the .env file
        # is safely sourceable.
        script = (
            "set -eu;"
            "python3 -c 'import json,sys,os;"
            "d=json.load(sys.stdin);"
            "p=d[\"params\"];"
            "raw=d[\"raw\"];"
            "shq=lambda s: chr(39)+s.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))+chr(39);"
            "open(\"/tmp/kasm_query.json.tmp\",\"w\").write(json.dumps(d, indent=2));"
            "os.rename(\"/tmp/kasm_query.json.tmp\",\"/tmp/kasm_query.json\");"
            "lines=[f\"KASM_Q_{k.upper()}={shq(v)}\" for k,v in p.items()];"
            "lines.append(f\"KASM_QUERY={shq(raw)}\");"
            "lines.append(\"KASM_QUERY_KEYS=\"+shq(\" \".join(p.keys())));"
            "open(\"/tmp/kasm_query.env.tmp\",\"w\").write(\"\\n\".join(lines)+\"\\n\");"
            "os.rename(\"/tmp/kasm_query.env.tmp\",\"/tmp/kasm_query.env\")'"
        )
        proc = _docker(
            "exec", "-i", container_id, "sh", "-c", script,
            stdin=json_payload.encode(),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"fallback writer failed (rc={proc.returncode}): "
                f"{proc.stderr.decode(errors='replace')[:200]}"
            )


# ---------------------------------------------------------------------------
# TLS (dual-stack: same port serves HTTP and HTTPS, picked by first-byte sniff)
# ---------------------------------------------------------------------------
def _ensure_self_signed_cert() -> ssl.SSLContext | None:
    """Generate a long-lived self-signed cert in TLS_CERT_DIR if absent, and
    return a server-side SSLContext. Returns None if TLS is disabled."""
    if not TLS_ENABLED:
        return None
    TLS_CERT_DIR.mkdir(parents=True, exist_ok=True)
    if not (TLS_CERT.exists() and TLS_KEY.exists()):
        log.info("generating self-signed cert at %s with SAN=%s", TLS_CERT, TLS_CERT_SAN)
        # CN is legacy-only — browsers check SAN. Default SAN keeps the
        # cert generic; override with TLS_CERT_SAN to add LAN names.
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
                "-keyout", str(TLS_KEY), "-out", str(TLS_CERT),
                "-days", "3650", "-nodes",
                "-subj", "/CN=kasm2-control-center",
                "-addext", f"subjectAltName={TLS_CERT_SAN}",
            ],
            check=True, capture_output=True,
        )
        TLS_KEY.chmod(0o600)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(TLS_CERT), keyfile=str(TLS_KEY))
    # Best-effort modern defaults; let stdlib pick ciphers.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class DualStackHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that auto-detects TLS on accept.

    The first byte of a TLS record is 0x16 (handshake). HTTP requests start
    with an ASCII method letter ('G', 'P', 'O', 'H', 'D'). Peeking one byte
    tells us which side to wrap. We don't consume the byte — the kernel
    re-delivers it once we either wrap_socket() (TLS path) or hand the raw
    socket to BaseHTTPRequestHandler (HTTP path).
    """
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, ssl_ctx: ssl.SSLContext | None):
        super().__init__(addr, handler)
        self.ssl_ctx = ssl_ctx

    def get_request(self):
        sock, addr = super().get_request()
        if self.ssl_ctx is None:
            return sock, addr
        try:
            sock.settimeout(5.0)
            first = sock.recv(1, socket.MSG_PEEK)
        except OSError:
            return sock, addr
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass
        if first == b"\x16":
            try:
                sock = self.ssl_ctx.wrap_socket(sock, server_side=True)
            except (ssl.SSLError, OSError) as e:
                log.warning("TLS handshake from %s failed: %s", addr, e)
                try:
                    sock.close()
                finally:
                    raise
        return sock, addr


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
_SESSION_PATH_RE = re.compile(r"^/api/session/([A-Za-z0-9_]+)/params/?$")
_SLOT_PROXY_RE   = re.compile(r"^/slot/(\d+)(?:/(.*))?$")

# `host.docker.internal` is added via `extra_hosts` in compose so the
# control-center can reach sibling pool containers (which publish on the
# host) without sharing a docker network. Same hop the readiness probe
# uses.
_PROXY_UPSTREAM_HOST = os.environ.get("PROXY_UPSTREAM_HOST", "host.docker.internal")
# Headers stripped when reframing the request for the upstream slot:
# hop-by-hop per RFC 7230 + Host (we rewrite it). `Upgrade` and
# `Connection` are technically hop-by-hop but MUST be forwarded for
# WebSocket — they signal the upgrade intent to the upstream. So they're
# absent from this set even though it would be tempting to include them.
_HOP_BY_HOP = frozenset({
    "host",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding",
})


class Handler(BaseHTTPRequestHandler):
    server_version = "control-center/0.3"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    # -- helpers --------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("request body too large")
        data = self.rfile.read(length)
        if not data:
            return {}
        return json.loads(data.decode("utf-8"))

    def _redirect_host(self) -> str:
        if PUBLIC_HOST:
            return PUBLIC_HOST
        h = self.headers.get("Host", "").strip()
        if not h:
            return "localhost"
        if h.startswith("["):
            end = h.find("]")
            return h[: end + 1] if end != -1 else h
        if ":" in h:
            return h.rsplit(":", 1)[0]
        return h

    def _request_scheme(self) -> str:
        """Did this request arrive over TLS? Used to pick the iframe scheme
        so clipboard delegation stays valid (secure context chain)."""
        xfp = self.headers.get("X-Forwarded-Proto", "").strip().lower()
        if xfp in ("http", "https"):
            return xfp
        return "https" if isinstance(self.request, ssl.SSLSocket) else "http"

    # -- slot reverse proxy --------------------------------------------
    def _maybe_proxy_slot(self) -> bool:
        """If the request path is `/slot/<idx>/*`, forward the whole
        connection (HTTP request + body, or WebSocket frames) to the
        slot's KasmVNC and return True. Returns False if not a slot path,
        letting the caller fall through to its normal routing."""
        m = _SLOT_PROXY_RE.match(self.path.split("?", 1)[0])
        if not m:
            return False
        idx_str, sub_path = m.group(1), m.group(2) or ""
        try:
            idx = int(idx_str)
        except ValueError:
            self.send_error(404, "bad slot index")
            return True
        if not (0 <= idx < len(_slots)):
            self.send_error(404, "slot out of range")
            return True
        slot = _slots[idx]

        # Open the upstream TCP connection. Plain HTTP — KasmVNC serves
        # HTTP and TLS on the same port, but we go HTTP since this hop
        # is container-to-container on the docker host.
        try:
            upstream = socket.create_connection(
                (_PROXY_UPSTREAM_HOST, slot.host_port), timeout=10.0,
            )
        except OSError as e:
            log.warning("slot %d: upstream connect failed: %s", idx, e)
            self.send_error(502, f"slot {idx} unreachable: {e}")
            return True

        try:
            # Rebuild the request line. The upstream sees `/<sub>?<qs>`
            # rather than `/slot/<idx>/<sub>?<qs>` — i.e., we strip our
            # routing prefix the same way a reverse proxy would.
            qs = ""
            if "?" in self.path:
                qs = "?" + self.path.split("?", 1)[1]
            forwarded_path = "/" + sub_path + qs

            request = (f"{self.command} {forwarded_path} HTTP/1.1\r\n").encode()
            for name, value in self.headers.items():
                if name.lower() in _HOP_BY_HOP:
                    continue
                request += f"{name}: {value}\r\n".encode()
            # Rewrite Host to the upstream's address — KasmVNC ignores it
            # but proxies in between (none today, but possible) shouldn't
            # see the control-center's Host.
            request += f"Host: {_PROXY_UPSTREAM_HOST}:{slot.host_port}\r\n".encode()
            request += b"\r\n"
            upstream.sendall(request)

            # Forward request body if present. We trust Content-Length
            # (no chunked decoding here — KasmVNC clients don't send
            # chunked bodies, and adding chunked support is out of scope).
            cl = int(self.headers.get("Content-Length", "0") or 0)
            if cl > 0:
                remaining = cl
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    upstream.sendall(chunk)
                    remaining -= len(chunk)

            # From here on, just pipe bytes in both directions. This
            # works for: a normal HTTP response (server sends status +
            # headers + body, then EOF or keep-alive), AND a WebSocket
            # upgrade (server sends 101 + headers, then opaque frames in
            # both directions until either side closes). The handler's
            # response cycle is hijacked — set close_connection so the
            # stdlib doesn't try to send anything else on this socket.
            self._pipe_until_close(self.connection, upstream)
        finally:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                upstream.close()
            except OSError:
                pass
            self.close_connection = True
        return True

    @staticmethod
    def _pipe_until_close(a: socket.socket, b: socket.socket) -> None:
        # 5-minute idle timeout. KasmVNC sends frequent frames once a
        # client is connected, so a real idle here means the user closed
        # the tab — safe to tear down.
        socks = [a, b]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 300.0)
                if not r:
                    return
                eof = False
                for s in r:
                    try:
                        data = s.recv(65536)
                    except OSError:
                        return
                    if not data:
                        eof = True
                        break
                    other = b if s is a else a
                    try:
                        other.sendall(data)
                    except OSError:
                        return
                if eof:
                    return
        except Exception as e:
            log.debug("proxy pipe ended: %s", e)

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self._maybe_proxy_slot():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {
                "ok": _pool_ready.is_set(),
                "pool_size": VIEWER_POOL_SIZE,
                "pool_ready": _pool_ready.is_set(),
                "pool_image": POOL_IMAGE,
                "pool_instance": POOL_INSTANCE,
                "viewer_port_range": (
                    f"{VIEWER_PORT_RANGE_START}-{VIEWER_PORT_RANGE_START + VIEWER_POOL_SIZE - 1}"
                ),
                "kasm_https": KASM_SVC_HTTPS_VALUE,
                "public_host_override": PUBLIC_HOST or None,
                "slots": [
                    {"idx": s.idx, "name": s.container_name,
                     "id": s.container_id[:12], "host_port": s.host_port}
                    for s in _slots
                ],
            })
            return

        if parsed.path == "/api/slots":
            # Pool size + slot list, no passwords. The page calls this on
            # load to know how many slot chips to render. Detailed connect
            # info (incl. vnc_pw + viewer_url) is returned per-slot by
            # POST /api/session.
            self._send_json(200, {
                "pool_size": VIEWER_POOL_SIZE,
                "pool_ready": _pool_ready.is_set(),
                "slots": [
                    {"idx": s.idx, "name": s.container_name,
                     "container_short": s.container_id[:12],
                     "host_port": s.host_port}
                    for s in _slots
                ],
            })
            return

        if parsed.path == "/":
            self._serve_static("index.html")
            return
        if parsed.path.startswith("/static/"):
            self._serve_static(parsed.path[len("/static/"):])
            return

        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if self._maybe_proxy_slot():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/session":
            self._handle_lease()
            return
        m = _SESSION_PATH_RE.match(parsed.path)
        if m:
            self._handle_update_params(m.group(1))
            return
        self.send_error(404, "not found")

    def do_HEAD(self) -> None:  # noqa: N802
        if self._maybe_proxy_slot():
            return
        if self.path.startswith("/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self.end_headers()

    # -- handlers -------------------------------------------------------
    def _serve_static(self, rel: str) -> None:
        safe = (STATIC_DIR / rel).resolve()
        try:
            safe.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(403, "forbidden")
            return
        if not safe.is_file():
            self.send_error(404, "not found")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".png":  "image/png",
            ".svg":  "image/svg+xml",
            ".json": "application/json",
        }.get(safe.suffix, "application/octet-stream")
        data = safe.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_lease(self) -> None:
        if not _pool_ready.is_set():
            self._send_json(503, {"error": "pool not ready, retry shortly"})
            return
        try:
            body = self._read_json()
        except Exception as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return
        params = body.get("params") or {}
        if not isinstance(params, dict):
            self._send_json(400, {"error": "params must be an object"})
            return
        # `url` from the spawn body is treated as a shortcut for
        # `open_url` — the desktop is already up, so the in-container
        # on_query_update.sh hook is what handles opening it.
        url = str(body.get("url") or "").strip()
        if url and "open_url" not in params:
            params = {**params, "open_url": url}
        # `desktop` is currently advisory (only ubuntu is pooled). We accept
        # it for forward-compat but don't gate on it.
        slot_req = body.get("slot")
        slot_idx: int | None = None
        if slot_req is not None:
            try:
                slot_idx = int(slot_req)
            except (TypeError, ValueError):
                self._send_json(400, {"error": "slot must be an integer"})
                return
        try:
            slot = _lease_slot(slot_idx)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        except Exception as e:
            self._send_json(503, {"error": str(e)})
            return
        try:
            write_query_to_container(slot.container_id, params)
        except Exception as e:
            log.exception("param write failed")
            self._send_json(502, {"error": f"slot write failed: {e}"})
            return

        scheme = self._request_scheme()
        # NoVNC's `path` URL param picks the WebSocket path. Default is the
        # bare string `websockify` which it ROOT-relatives — so without an
        # explicit override it would connect to `wss://<host>/websockify` and
        # bypass our proxy prefix. Pin it to the proxied path.
        viewer_ws_path = f"slot/{slot.idx}/websockify"
        viewer_qs = urlencode([
            ("password", slot.vnc_pw),
            ("autoconnect", "1"),
            ("path", viewer_ws_path),
            ("resize", VIEWER_RESIZE),
            ("quality", VIEWER_QUALITY),
            ("compression", VIEWER_COMPRESSION),
            ("dynamic_quality_min", VIEWER_DYNAMIC_QUALITY_MIN),
            ("dynamic_quality_max", VIEWER_DYNAMIC_QUALITY_MAX),
            ("prefer_local_cursor", VIEWER_PREFER_LOCAL_CURSOR),
            ("clipboard_up", VIEWER_CLIPBOARD_UP),
            ("clipboard_down", VIEWER_CLIPBOARD_DOWN),
            ("clipboard_seamless", VIEWER_CLIPBOARD_SEAMLESS),
        ])
        # Iframe URL: same-origin reverse proxy at `/slot/<idx>/`. The
        # control-center forwards HTTP + WebSocket Upgrade requests under
        # that prefix to the slot's KasmVNC on host.docker.internal:<port>.
        # Same origin as the parent means same cert, no cross-origin cert
        # prompt, and the clipboard delegation chain just works whenever
        # the parent page itself is a secure context (HTTPS or localhost).
        viewer_url = f"/slot/{slot.idx}/?{viewer_qs}"
        viewer_url_fallback = None
        self._send_json(200, {
            "session_id": slot.container_id,
            "container_id": slot.container_id,
            "container_short": slot.container_id[:12],
            "container_name": slot.container_name,
            "viewer_url": viewer_url,
            "viewer_url_fallback": viewer_url_fallback,
            "vnc_pw": slot.vnc_pw,
            "image": slot.image,
            "host_port": slot.host_port,
            "slot": slot.idx,
        })

    def _handle_update_params(self, container_id: str) -> None:
        try:
            body = self._read_json()
        except Exception as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return
        # Prefer body["params"] when present (the normal JS payload). Fall
        # back to treating the whole body as the params dict for callers
        # that POST `{theme: 'dark', ...}` directly. We can't use `or body`
        # because an empty params dict {} is falsy — that fell through and
        # produced a misleading "params: {}" write.
        if "params" in body:
            params = body["params"]
        else:
            params = body
        if not isinstance(params, dict):
            self._send_json(400, {"error": "params must be an object"})
            return
        slot = _find_slot(container_id)
        if slot is None and not _container_exists(container_id):
            self._send_json(404, {"error": "no such container"})
            return
        try:
            write_query_to_container(container_id, params)
        except Exception as e:
            log.exception("param write failed")
            self._send_json(502, {"error": str(e)})
            return
        self._send_json(200, {"ok": True, "container_id": container_id, "params": build_passthrough(params)})


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def main() -> None:
    if not STATIC_DIR.is_dir():
        raise SystemExit(f"STATIC_DIR not found: {STATIC_DIR}")

    atexit.register(_shutdown_pool)

    def _sigterm(signum, frame):  # noqa: ANN001
        log.info("got signal %d, shutting down", signum)
        _shutdown_pool()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    # Build pool BEFORE listening so `/api/session` always sees a ready pool.
    _init_pool()

    ssl_ctx = _ensure_self_signed_cert()
    srv = DualStackHTTPServer(("0.0.0.0", CONTROL_CENTER_PORT), Handler, ssl_ctx)
    log.info(
        "control-center listening on 0.0.0.0:%d (tls=%s pool=%d image=%s instance=%s public_host=%s)",
        CONTROL_CENTER_PORT,
        "dual" if ssl_ctx else "off",
        VIEWER_POOL_SIZE, POOL_IMAGE, POOL_INSTANCE,
        PUBLIC_HOST or "<from Host header>",
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
