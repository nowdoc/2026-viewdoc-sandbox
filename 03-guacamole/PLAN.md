# Prototype 03 — Apache Guacamole URL-to-Viewer Dispatcher

## Goal recap

User → `http://<host>:7081/?url=<URL>` → dispatcher classifies → spawns
viewer container (VNC, internal-only) → registers connection in Guacamole
via signed json-auth blob → 302 to Guacamole web client → connection
auto-opens with the file already loaded.

## Architecture

```
                      ┌────────────────────────────────────────────────────┐
                      │ Host (orbstack / docker)                           │
                      │                                                    │
   browser ──GET/?url─▶ dispatcher (FastAPI, 0.0.0.0:7081)                 │
       ▲              │     │                                              │
       │ 302          │     │ 1. classify(url) → pdf|image|av              │
       │ Location uses│     │ 2. docker run viewer  (NO host port for      │
       │ Host header  │     │    5900 — attaches to guac_net only)         │
       │              │     │ 3. mint json-auth blob (HMAC + AES-128-CBC)  │
       │              │     │ 4. POST /api/tokens → authToken              │
       │              │     │ 5. build /#/client/<b64>?token=<tok>         │
       │              │     ▼                                              │
       └──── client ──▶ guacamole/guacamole web (0.0.0.0:7080) ── guacd ──┐│
                      │      (json-auth ext loaded by JSON_SECRET_KEY)   ││
                      │                                                  ││
                      │  guac-viewer-<id>     (NO host port mapping)     ││
                      │   xvfb+x11vnc+xpdf/feh/vlc, EXPOSE 5900  ◀───────┘│
                      └────────────────────────────────────────────────────┘
```

Network:
- `guac_net` — bridge network shared by guacd + guacamole + dispatcher +
  every viewer container.
- guacd reaches each viewer by **docker container name** on `guac_net`. No
  host port is published for VNC, so viewers are unreachable from the LAN.

Host ports (both bound on `0.0.0.0` — reachable from other machines on the
LAN, not just localhost):
- `7081/tcp` → dispatcher
- `7080/tcp` → Guacamole web

## Why Guacamole over alternatives

| Aspect                 | Guacamole                                        | neko/xpra                       |
|------------------------|--------------------------------------------------|---------------------------------|
| Browser client         | HTML5 / canvas, no plugin                        | HTML5 (neko) / HTML5 (xpra)     |
| Auth model             | Built-in users + extensions (LDAP, OIDC, JSON…)  | Token / room based              |
| Protocols              | VNC, RDP, SSH, Kubernetes, telnet                | Custom (neko=WebRTC, xpra=ws)   |
| Multi-tenant           | Yes (per-connection ACL)                         | Yes                             |
| Pain points for MVP    | DB-less dynamic connections require json-auth    | Simpler URL-tokens              |
| Footprint              | guacd + tomcat (~400MB total)                    | smaller                         |

We pick Guacamole here mainly as a *comparison data point* against
neko-rooms (prototype 01) and xpra (prototype 02). It is the most
"enterprise" of the three.

## Key decisions

1. **json-auth extension instead of REST connection management.** The
   official `guacamole/guacamole:1.5.5` image auto-loads
   `guacamole-auth-json-1.5.5.jar` when the `JSON_SECRET_KEY` env var is
   set. The dispatcher signs and AES-128-CBC-encrypts a small JSON blob
   describing the one VNC connection it just spawned and POSTs that blob
   to `/api/tokens`. Guacamole creates a single-use session pre-bound to
   that connection. No database, no admin user, no `/etc/guacamole` writes
   at runtime, no REST `POST /connections` (which 403s under basic
   user-mapping anyway).
2. **Connection identifier scheme.** Dispatcher names the connection
   `viewer-<8hex>` and builds the client identifier as
   `base64(name + '\0' + 'c' + '\0' + 'json')`. The `json` data source is
   the json-auth extension's identifier.
3. **Viewer images.** Single multi-purpose Debian-slim image at
   `images/viewer/` with `xvfb + x11vnc + xpdf + feh + vlc`. Entrypoint
   picks the viewer based on `$VIEWER`. One image is simpler than three
   and only ~280 MB.
4. **guacd → viewer reachability.** Viewer containers attach to `guac_net`
   and guacd resolves them by container name through docker's embedded
   DNS. No host port mapping, no `host.docker.internal`, no
   `--add-host=host-gateway`. This also means the VNC servers are NOT
   exposed to the LAN — which is correct, because `x11vnc -nopw` has no
   password.
5. **Network accessibility.** Both host port mappings bind on `0.0.0.0`
   (explicit in `docker-compose.yml`). The dispatcher's 302 `Location`
   header is built from the incoming request's `Host` header, so users
   land on the same hostname/IP they used. `PUBLIC_HOST` env var overrides
   this for deployments where `Host` is rewritten by a proxy. localhost
   is NEVER hardcoded.
6. **Cleanup.** Viewer containers use `--rm` and are labelled
   `guac-viewer=true`; dispatcher exposes `GET /cleanup` (best-effort).

## Risks / known pain

- **No VNC password.** Viewer VNC is `nopw` and reachable only on
  `guac_net`. Anyone with `docker exec` or a container on the same
  network can connect. Acceptable since the threat model is LAN-only and
  the viewer process dies with the container.
- **json-auth shared secret in env.** `JSON_SECRET_KEY` is duplicated in
  the `guacamole` and `dispatcher` services. Change to a real secret for
  any deployment beyond the LAN demo.
- **No idle reaper.** Hitting `?url=X` twice spawns two containers and
  leaves the first one running until the user closes the browser tab
  (`--exit-with-children=yes` is not how this image is built; the
  entrypoint just waits on the viewer PID). Use `/cleanup` between tests.
- **VLC under Xvfb without audio.** VLC will play video but audio is
  dropped (no PulseAudio in the viewer image). Acceptable for an MVP.

## Outcome (post-build)

End-to-end verification with the host's LAN IP (substitute `$HOST_IP`):

```
$ curl -s -o /dev/null -D - "http://$HOST_IP:7081/?url=https://www.africau.edu/images/default/sample.pdf" | grep -iE '^(HTTP|location:)'
HTTP/1.1 302 Found
location: http://<LAN-IP>:7080/guacamole/#/client/dmlld2VyLTQwN2ExN2FlAGMAanNvbg?token=BD26085A...
```

The Location echoes the LAN IP — no `localhost` substitution. With a
spoofed `Host: viewer.example.com:7081` the dispatcher emits
`location: http://viewer.example.com:7080/...` instead, and with
`PUBLIC_HOST=guac.example.org` set, the env var wins:
`location: http://guac.example.org:7080/...`.

Viewer containers (`docker ps --filter label=guac-viewer=true`) show
`5900/tcp` only in the EXPOSE column — never `0.0.0.0:NNNN->5900/tcp`. The
host has nothing listening on `5900`.
