#!/usr/bin/env python3
"""URL-to-viewer dispatcher for the kasm-standalone prototype.

Flow:
  GET /?url=<asset_url>
    1. Classify the URL by extension using mapping.yaml.
    2. Pick a free host port from VIEWER_PORT_RANGE_START..END.
    3. Generate a random VNC_PW.
    4. `docker run -d --rm -p <port>:6901 -e KASM_SVC_HTTPS=disabled \\
                   -e VNC_PW=<pw> -e <rule.env...> <rule.image>`
    5. Poll http://host.docker.internal:<port>/ until 200 OK (plain HTTP,
       no TLS, because KASM_SVC_HTTPS=disabled flips KasmVNC off TLS).
    6. 302 redirect to:
         http://<request-host>:<port>/?password=<pw>&autoconnect=1&resize=remote
       where <request-host> is the Host header (with port stripped) or
       PUBLIC_HOST override.

  GET /healthz -> 200 json

We deliberately keep this stdlib-only apart from PyYAML for the mapping file.
The docker CLI is invoked via subprocess; the dispatcher mounts the host's
/var/run/docker.sock so spawned viewers run as SIBLINGS on the host engine.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shlex
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

# ---------------------------------------------------------------------------
# Config (env)
# ---------------------------------------------------------------------------
DISPATCHER_PORT = int(os.environ.get("DISPATCHER_PORT", "6081"))

# Container-internal NoVNC/KasmVNC port. kasmweb/* images all expose 6901.
KASM_CONTAINER_PORT = int(os.environ.get("KASM_CONTAINER_PORT", "6901"))

# Ephemeral host-port window we hand out for viewer containers.
# We pin host ports inside a small range (rather than letting docker pick from
# the kernel ephemeral range) so operators can firewall a predictable window.
# Defaults match the brief: 6082-6099. Stay clear of 3000, 7080-81, 8080-81,
# 9081, 9082-9099, 32768-32770 (in use by sibling prototypes on this host).
VIEWER_PORT_RANGE_START = int(os.environ.get("VIEWER_PORT_RANGE_START", "6082"))
VIEWER_PORT_RANGE_END = int(os.environ.get("VIEWER_PORT_RANGE_END", "6099"))

# If set, override the host part of the 302 Location. Useful behind a
# reverse proxy that mangles Host. When unset (default) we derive from the
# request's Host header so a LAN client at 192.168.x.y stays on that host.
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()

# Container-side TLS opt-out for KasmVNC. Documented in PLAN.md.
KASM_SVC_HTTPS_VALUE = os.environ.get("KASM_SVC_HTTPS_VALUE", "disabled")

# Per-container resource caps. KasmVNC + Chromium is hungry on first paint.
VIEWER_MEMORY = os.environ.get("VIEWER_MEMORY", "2g")
VIEWER_CPUS = os.environ.get("VIEWER_CPUS", "2.0")

# Wait-ready timeout for the spawned viewer. kasmweb/chromium cold-start runs
# ~15-25 s, kasmweb/vlc is faster. Be generous.
READY_TIMEOUT_S = float(os.environ.get("READY_TIMEOUT_S", "60"))

# Optional docker network for the viewer containers. If empty, docker default
# bridge is used.
VIEWER_NETWORK = os.environ.get("VIEWER_NETWORK", "")

# Path to the data-driven mapping file. Mounted/baked into the dispatcher image.
MAPPING_PATH = Path(os.environ.get("MAPPING_PATH", "/app/mapping.yaml"))

log = logging.getLogger("dispatcher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# ---------------------------------------------------------------------------
# Mapping (ext -> image+env) loaded once at startup.
# ---------------------------------------------------------------------------
class Rule:
    __slots__ = ("kind", "extensions", "image", "env", "default")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.kind: str = str(raw.get("kind", "doc"))
        self.extensions: list[str] = [
            e.lower() for e in (raw.get("extensions") or [])
        ]
        self.image: str = str(raw["image"])
        self.env: dict[str, str] = dict(raw.get("env") or {})
        self.default: bool = bool(raw.get("default", False))


def load_rules(path: Path) -> list[Rule]:
    data = yaml.safe_load(path.read_text())
    rules = [Rule(r) for r in (data.get("rules") or [])]
    if not any(r.default for r in rules):
        log.warning("mapping.yaml has no default rule; unknown extensions will 400")
    return rules


def classify(url: str, rules: list[Rule]) -> Rule | None:
    """Find the first rule whose extensions list matches the URL's path ext.

    Falls back to the rule marked `default: true`. Returns None only if no
    default rule was provided AND no extension matched.
    """
    path = urlparse(url).path.lower()
    # Strip query/fragment was already done by urlparse; pull the trailing ext.
    dot = path.rfind(".")
    ext = path[dot:] if dot != -1 else ""
    for r in rules:
        if r.default:
            continue
        if ext in r.extensions:
            return r
    for r in rules:
        if r.default:
            return r
    return None


# ---------------------------------------------------------------------------
# Container spawn helpers
# ---------------------------------------------------------------------------
def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a docker CLI command. We shell out to the static `docker` binary
    baked into the dispatcher image (see dispatcher/Dockerfile)."""
    cmd = ["docker", *args]
    log.debug("exec: %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


_port_lock = threading.Lock()


def _pick_free_port() -> int:
    """Pick a free port in [VIEWER_PORT_RANGE_START, VIEWER_PORT_RANGE_END].

    Strategy: ask docker which host ports are currently published, skip those,
    AND additionally try to bind() the candidate locally — this catches
    non-docker listeners (e.g. another sibling prototype) too.
    """
    proc = _docker("ps", "--format", "{{.Ports}}", "--no-trunc", check=False)
    in_use: set[int] = set()
    for line in proc.stdout.splitlines():
        # Lines look like: "0.0.0.0:6082->6901/tcp, :::6082->6901/tcp"
        for chunk in line.split(","):
            chunk = chunk.strip()
            if "->" not in chunk:
                continue
            left = chunk.split("->", 1)[0]
            if ":" in left:
                try:
                    in_use.add(int(left.rsplit(":", 1)[1]))
                except ValueError:
                    pass
    for p in range(VIEWER_PORT_RANGE_START, VIEWER_PORT_RANGE_END + 1):
        if p in in_use:
            continue
        # Second-layer check: can we actually bind it locally? If not, some
        # non-docker process is holding it.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", p))
        except OSError:
            continue
        finally:
            s.close()
        return p
    raise RuntimeError(
        f"no free port in {VIEWER_PORT_RANGE_START}-{VIEWER_PORT_RANGE_END}"
    )


# Reserved query params consumed by the dispatcher itself. Anything else the
# user puts on the URL is forwarded to the spawned container as env vars.
_RESERVED_QUERY_KEYS = {"desktop", "url", "u"}

# Max sizes — defensive, prevents env-blowup if the user crafts a huge URL.
_MAX_PASSTHROUGH_KEYS = 32
_MAX_PASSTHROUGH_VALUE_LEN = 4096
_PASSTHROUGH_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def build_passthrough_env(raw_query: str) -> dict[str, str]:
    """Capture user-supplied query params (everything but the reserved keys)
    and turn them into env vars the spawned container can read.

    For each param `foo=bar` (after key sanitization):
      KASM_Q_FOO = "bar"

    Also exposes:
      KASM_QUERY        = full filtered raw query string (re-encoded)
      KASM_QUERY_KEYS   = space-separated list of forwarded key names

    Why two formats: KASM_Q_<KEY> is convenient for shell scripts inside the
    container; KASM_QUERY preserves the original URL-encoded form for code
    that wants to re-parse it.
    """
    parsed = parse_qs(raw_query, keep_blank_values=True)
    out: dict[str, str] = {}
    keys: list[str] = []
    kept_pairs: list[tuple[str, str]] = []
    for k, vals in parsed.items():
        if k in _RESERVED_QUERY_KEYS:
            continue
        if not _PASSTHROUGH_KEY_RE.match(k):
            log.debug("skipping unsafe passthrough key: %r", k)
            continue
        if len(out) >= _MAX_PASSTHROUGH_KEYS:
            break
        v = vals[0] if vals else ""
        if len(v) > _MAX_PASSTHROUGH_VALUE_LEN:
            v = v[:_MAX_PASSTHROUGH_VALUE_LEN]
        env_key = f"KASM_Q_{k.upper()}"
        out[env_key] = v
        keys.append(k)
        kept_pairs.append((k, v))
    if kept_pairs:
        # Re-encode so the consumer sees a normalised form.
        from urllib.parse import urlencode
        out["KASM_QUERY"] = urlencode(kept_pairs)
        out["KASM_QUERY_KEYS"] = " ".join(keys)
    return out


def spawn_viewer(rule: Rule, url: str, passthrough_env: dict[str, str] | None = None) -> tuple[str, int, str, str]:
    """`docker run` a kasmweb container for this URL.

    Returns (container_id, host_port, vnc_password, container_name).

    We:
      - generate a fresh 16-hex-char password for VNC_PW (so the redirect's
        ?password=… is unguessable).
      - set KASM_SVC_HTTPS=<value> to disable TLS on the container's NoVNC
        endpoint (otherwise the user gets a self-signed-cert warning).
      - publish container port 6901 on a host port picked from our range,
        WITHOUT a bind-IP prefix → docker binds on 0.0.0.0 and :: (LAN
        reachable).
      - label `dispatcher.session=<uuid>` so cleanup (`docker rm -f -l
        dispatcher.session=…`) is trivial.
    """
    session_id = uuid.uuid4().hex
    name = f"kasm-viewer-{session_id[:10]}"
    vnc_pw = secrets.token_hex(8)  # 16 hex chars

    with _port_lock:
        host_port = _pick_free_port()

    args: list[str] = [
        "run", "-d", "--rm",
        "--name", name,
        "--label", f"dispatcher.session={session_id}",
        "--label", "dispatcher=kasm-standalone",
        "-p", f"{host_port}:{KASM_CONTAINER_PORT}",  # no bind IP -> 0.0.0.0
        "-e", f"VNC_PW={vnc_pw}",
        "-e", f"KASM_SVC_HTTPS={KASM_SVC_HTTPS_VALUE}",
        # Audio is wanted (VLC, video). KASM_SVC_AUDIO=1 (default) starts the
        # jsmpeg audio websocket inside the container; KasmVNC multiplexes it
        # over the main 6901 port so no extra publish needed.
        # Disable uploads/downloads to cut cold-start; they're irrelevant to
        # a view-only flow.
        "-e", "KASM_SVC_UPLOADS=disabled",
        "-e", "KASM_SVC_DOWNLOADS=disabled",
        "--shm-size=512m",  # chromium wants more than the default 64MB
        f"--memory={VIEWER_MEMORY}",
        f"--cpus={VIEWER_CPUS}",
    ]
    # Per-rule env (LAUNCH_URL / APP_ARGS / etc) with {url} substituted.
    for k, v in rule.env.items():
        args += ["-e", f"{k}={v.format(url=url)}"]
    # User-supplied passthrough env from the dispatcher's query string.
    for k, v in (passthrough_env or {}).items():
        args += ["-e", f"{k}={v}"]
    if VIEWER_NETWORK:
        args += ["--network", VIEWER_NETWORK]
    args.append(rule.image)

    proc = _docker(*args)
    container_id = proc.stdout.strip()
    if not container_id:
        raise RuntimeError(f"docker run produced no id: {proc.stderr!r}")
    log.info(
        "spawned %s (%s) kind=%s image=%s url=%s host_port=%d",
        name, container_id[:12], rule.kind, rule.image, url, host_port,
    )
    return container_id, host_port, vnc_pw, name


def _wait_ready(host_port: int, timeout: float) -> bool:
    """Poll the spawned container until it returns HTTP 200 on / (plain HTTP).

    We poll `host.docker.internal:<host_port>/` from inside the dispatcher
    container — that resolves to the host network via compose's
    extra_hosts: host-gateway.

    Returns True on success, False on timeout. We deliberately don't raise:
    a slow boot shouldn't make the dispatcher 502 if the page is still
    likely to be up by the time the browser follows the 302.
    """
    deadline = time.time() + timeout
    host = "host.docker.internal"
    last_err: str | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, host_port), timeout=2.0) as s:
                s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
                data = s.recv(64)
                if data.startswith(b"HTTP/") and b" 200" in data[:20]:
                    return True
                # KasmVNC may 302 redirect (e.g. to /vnc.html). Accept any 2xx/3xx.
                if data.startswith(b"HTTP/") and (b" 30" in data[:20] or b" 20" in data[:20]):
                    return True
                last_err = data[:40].decode(errors="replace")
        except Exception as e:
            last_err = str(e)
        time.sleep(0.5)
    log.warning("port %d not ready within %.1fs (last=%s)", host_port, timeout, last_err)
    return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "kasm-dispatcher/0.1"

    # rules and version are set on the class after load_rules()
    rules: list[Rule] = []

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib override
        log.info("%s - %s", self.address_string(), fmt % args)

    def _find_desktop_rule(self, name: str) -> "Rule | None":
        """Find a rule whose `kind` is 'desktop' AND its first extension entry
        matches the requested desktop name (e.g. 'ubuntu')."""
        for r in self.rules:
            if r.kind == "desktop" and (name in r.extensions or r.default):
                return r
        return None

    def _redirect_host(self) -> str:
        """Pick the host part for the 302 Location.

        Priority: PUBLIC_HOST env override -> request Host header (port
        stripped) -> "localhost" last-resort. Mirrors xpra's logic.
        """
        if PUBLIC_HOST:
            return PUBLIC_HOST
        host_hdr = self.headers.get("Host", "").strip()
        if host_hdr:
            if host_hdr.startswith("["):
                end = host_hdr.find("]")
                if end != -1:
                    return host_hdr[: end + 1]
                return host_hdr
            if ":" in host_hdr:
                return host_hdr.rsplit(":", 1)[0]
            return host_hdr
        return "localhost"

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
        if self.path.startswith("/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            body = json.dumps({
                "ok": True,
                "kasm_container_port": KASM_CONTAINER_PORT,
                "viewer_port_range": f"{VIEWER_PORT_RANGE_START}-{VIEWER_PORT_RANGE_END}",
                "kasm_https": KASM_SVC_HTTPS_VALUE,
                "public_host_override": PUBLIC_HOST or None,
                "rules": [
                    {"kind": r.kind, "image": r.image, "extensions": r.extensions, "default": r.default}
                    for r in self.rules
                ],
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

        # /?desktop=<name> path: spawn a full kasmweb desktop image (e.g.
        # ubuntu-jammy-desktop). Optional ?url= alongside opens that asset
        # inside the desktop session via the wrapper's custom_startup.sh
        # (xdg-open after desktop_ready).
        desktop = (qs.get("desktop") or [""])[0].strip().lower()
        if desktop:
            rule = self._find_desktop_rule(desktop)
            if rule is None:
                self.send_error(400, f"no desktop rule for {desktop!r}")
                return
            url = (qs.get("url") or qs.get("u") or [""])[0]
        else:
            urls = qs.get("url") or qs.get("u")
            if not urls:
                body = (
                    b"<html><body><h1>kasm-standalone dispatcher</h1>"
                    b"<p>Usage: <code>/?url=&lt;asset_url&gt;</code> or "
                    b"<code>/?desktop=ubuntu</code></p>"
                    b"<p><a href='/healthz'>/healthz</a></p>"
                    b"</body></html>"
                )
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            url = urls[0]
            rule = classify(url, self.rules)
            if rule is None:
                self.send_error(400, f"no rule matched and no default; url={url}")
                return

        passthrough = build_passthrough_env(parsed.query)
        try:
            container_id, host_port, vnc_pw, name = spawn_viewer(rule, url, passthrough)
        except subprocess.CalledProcessError as e:
            log.exception("docker run failed")
            self.send_error(502, f"docker run failed: {e.stderr.strip()[:200]}")
            return
        except Exception as e:
            log.exception("spawn failed")
            self.send_error(500, f"spawn failed: {e}")
            return

        _wait_ready(host_port, READY_TIMEOUT_S)

        host = self._redirect_host()
        # NoVNC autoconnect: ?password=…&autoconnect=1&resize=remote drops the
        # user straight into the desktop without the login form.
        location = (
            f"http://{host}:{host_port}/"
            f"?password={vnc_pw}&autoconnect=1&resize=remote"
        )
        log.info("redirecting -> %s (container %s, kind=%s)", location, container_id[:12], rule.kind)
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Viewer-Container", container_id[:12])
        self.send_header("X-Viewer-Name", name)
        self.send_header("X-Viewer-Kind", rule.kind)
        self.send_header("X-Viewer-Image", rule.image)
        self.end_headers()


def main() -> None:
    Handler.rules = load_rules(MAPPING_PATH)
    log.info(
        "loaded %d rules from %s: %s",
        len(Handler.rules),
        MAPPING_PATH,
        [f"{r.kind}({','.join(r.extensions) or '*'} -> {r.image})" for r in Handler.rules],
    )
    srv = ThreadingHTTPServer(("0.0.0.0", DISPATCHER_PORT), Handler)
    log.info(
        "dispatcher listening on 0.0.0.0:%d (kasm_https=%s, public_host=%s, port_range=%d-%d)",
        DISPATCHER_PORT, KASM_SVC_HTTPS_VALUE,
        PUBLIC_HOST or "<from Host header>",
        VIEWER_PORT_RANGE_START, VIEWER_PORT_RANGE_END,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
