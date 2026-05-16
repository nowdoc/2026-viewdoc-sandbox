#!/usr/bin/env python3
"""Minimal control-center for the webtop bridge MVP.

Single fixed slot, no pool. Three endpoints:
  GET  /healthz       — liveness
  GET  /              — serve static page (iframe + URL watcher)
  GET  /static/*
  POST /api/params    — write outer-URL params into /tmp/webtop_query.*
                        inside the webtop container

HTTP only (LAN deployment should put a TLS-terminating proxy in front).
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse

PORT = int(os.environ.get("CONTROL_CENTER_PORT", "5087"))
WEBTOP_CONTAINER = os.environ.get("WEBTOP_CONTAINER", "webtop-bridge-webtop")
STATIC_DIR = Path(os.environ.get("STATIC_DIR", "/app/static"))
CERT_DIR = Path(os.environ.get("CERT_DIR", "/app/certs"))
CERT_SAN = os.environ.get(
    "CERT_SAN", "DNS:localhost,IP:127.0.0.1"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("control-center")

_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_MAX_KEYS = 32
_MAX_VAL_LEN = 4096


def sanitise(params: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in params.items():
        if not isinstance(k, str) or not _KEY_RE.match(k):
            continue
        s = str(v)
        if len(s) > _MAX_VAL_LEN:
            s = s[:_MAX_VAL_LEN]
        out[k] = s
        if len(out) >= _MAX_KEYS:
            break
    return out


def write_query(params: dict[str, str]) -> None:
    raw = urlencode(list(params.items()))
    payload = json.dumps({"params": params, "raw": raw}).encode()
    # -u 1000:1000 so the spawned hook child runs as the abc desktop user
    # (PUID=1000) and can attach to its X session / dbus.
    proc = subprocess.run(
        ["docker", "exec", "-i", "-u", "1000:1000",
         WEBTOP_CONTAINER, "/usr/local/bin/webtop-write-query"],
        input=payload, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker exec failed (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:200]}"
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "webtop-bridge/0.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel: str) -> None:
        path = (STATIC_DIR / rel).resolve()
        try:
            path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self.send_error(403, "forbidden")
        if not path.is_file():
            return self.send_error(404, "not found")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        p = urlparse(self.path).path
        if p == "/healthz":
            return self._json(200, {"ok": True, "container": WEBTOP_CONTAINER})
        if p == "/":
            return self._static("index.html")
        if p.startswith("/static/"):
            return self._static(p[len("/static/"):])
        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/params":
            return self.send_error(404, "not found")
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = json.loads(self.rfile.read(length).decode()) if length else {}
        except Exception as e:
            return self._json(400, {"error": f"bad json: {e}"})
        params = body["params"] if isinstance(body, dict) and "params" in body else body
        if not isinstance(params, dict):
            return self._json(400, {"error": "params must be an object"})
        clean = sanitise(params)
        try:
            write_query(clean)
        except Exception as e:
            log.exception("write failed")
            return self._json(502, {"error": str(e)})
        self._json(200, {"ok": True, "params": clean})


def ensure_cert() -> tuple[str, str]:
    """Generate a self-signed cert + key on first boot; reuse thereafter.

    HTTPS is mandatory here, not cosmetic: an HTTP parent page would make
    the embedded Selkies iframe a non-secure context (top-level cascade),
    and Selkies refuses to start in that case.
    """
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert = CERT_DIR / "cert.pem"
    key = CERT_DIR / "key.pem"
    if cert.exists() and key.exists():
        return str(cert), str(key)
    log.info("generating self-signed cert in %s (SAN=%s)", CERT_DIR, CERT_SAN)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert),
         "-days", "3650",
         "-subj", "/CN=webtop-bridge-cc",
         "-addext", f"subjectAltName={CERT_SAN}"],
        check=True, capture_output=True,
    )
    return str(cert), str(key)


def main() -> None:
    if not STATIC_DIR.is_dir():
        sys.exit(f"STATIC_DIR not found: {STATIC_DIR}")
    cert, key = ensure_cert()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    log.info("control-center listening on 0.0.0.0:%d (https, self-signed) → %s",
             PORT, WEBTOP_CONTAINER)
    srv.serve_forever()


if __name__ == "__main__":
    main()
