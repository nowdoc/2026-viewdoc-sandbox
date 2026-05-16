# guacamole-viewer prototype

One-asset-per-container viewer built on top of [Apache
Guacamole](https://guacamole.apache.org). A dispatcher receives `GET
/?url=<asset>`, spawns a disposable Debian container that runs Xvfb +
x11vnc + the right viewer (`xpdf` / `feh` / `vlc`) on the asset, mints a
signed json-auth blob describing the new VNC connection, exchanges it for
a Guacamole session token, and 302-redirects the browser to the Guacamole
HTML5 client URL with the token already attached.

## Components

| Component   | Path                          | Purpose                                                  |
|-------------|-------------------------------|----------------------------------------------------------|
| viewer img  | `images/viewer/Dockerfile`    | Debian + Xvfb + x11vnc + xpdf/feh/vlc                    |
| entrypoint  | `images/viewer/entrypoint.sh` | Downloads asset, starts X+VNC, launches viewer           |
| dispatcher  | `dispatcher/app.py`           | FastAPI service, `docker run`s viewers, mints json-auth  |
| compose     | `docker-compose.yml`          | guacd + guacamole web (json-auth enabled) + dispatcher   |

Guacamole's json-auth extension is enabled by passing `JSON_SECRET_KEY` to
the `guacamole/guacamole` image as an env var — the image's start script
auto-loads `/opt/guacamole/json/guacamole-auth-json-1.5.5.jar` when that
var is set. No `user-mapping.xml`, no database.

## Quick start

```bash
# Build the viewer image (used by the dispatcher per request).
docker build -t guac-viewer:latest images/viewer

# Build & start the stack.
docker compose up -d --build

# From any machine on the LAN:
curl -i "http://<host-ip>:7081/?url=https://www.africau.edu/images/default/sample.pdf"
# 302 Location: http://<host-ip>:7080/guacamole/#/client/<base64-id>?token=<token>
# then open that URL in a browser.
```

## Network accessibility

This prototype is designed to be reached from other machines on the LAN,
not just `localhost`. The deliberate choices are:

1. **Both host port mappings bind on `0.0.0.0`.** `docker-compose.yml`
   uses `0.0.0.0:7081:7081` (dispatcher) and `0.0.0.0:7080:8080`
   (Guacamole web). Verify with `docker port guac_dispatcher` and
   `docker port guac_web`.
2. **The 302 `Location` uses the incoming request's `Host` header,**
   replacing only the port with 7080. A client coming from
   `http://<your-host>:7081/?...` is redirected to
   `http://<your-host>:7080/guacamole/#/client/...`, never to
   `localhost`. Override with the `PUBLIC_HOST` env var if `Host` is
   unreliable in your deployment (e.g. behind certain reverse proxies).
3. **Viewer VNC ports stay internal.** Viewer containers do NOT publish
   port 5900 to the host. guacd reaches each viewer by its docker
   container name on the shared `guac_net` bridge network. This means
   the VNC servers are NOT exposed to the LAN — only the Guacamole web
   client is, which is the right boundary for a VNC-without-auth setup.

### Firewall

The host firewall must allow inbound TCP on:

- **`7081/tcp`** — the dispatcher.
- **`7080/tcp`** — the Guacamole web client (where the browser actually
  talks to once redirected).

Viewer VNC ports (5900 inside each viewer container) are **never** mapped
to the host, so no firewall rule is needed for them.

For UFW:

```bash
sudo ufw allow 7080/tcp
sudo ufw allow 7081/tcp
```

For firewalld:

```bash
sudo firewall-cmd --permanent --add-port=7080/tcp
sudo firewall-cmd --permanent --add-port=7081/tcp
sudo firewall-cmd --reload
```

## Environment

`docker-compose.yml` knobs on the dispatcher:

| Var                     | Default                            | Meaning                                                                          |
|-------------------------|------------------------------------|----------------------------------------------------------------------------------|
| `GUACAMOLE_INTERNAL_URL`| `http://guacamole:8080/guacamole`  | Where the dispatcher calls Guacamole's REST API (server-to-server).              |
| `PUBLIC_HOST`           | *(empty — uses request Host)*      | If set, overrides the host part of the 302 `Location`.                           |
| `GUAC_PUBLIC_PORT`      | `7080`                             | Port placed in the 302 `Location`.                                               |
| `GUAC_PUBLIC_PATH`      | `/guacamole`                       | Path prefix placed in the 302 `Location`.                                        |
| `JSON_SECRET_KEY`       | (32 hex chars, see compose)        | json-auth shared secret. MUST match the value on the guacamole container.        |
| `VIEWER_IMAGE`          | `guac-viewer:latest`               | Image `docker run` uses for each request.                                        |
| `VIEWER_NETWORK`        | `guac_net`                         | Docker network shared by guacd, guacamole, dispatcher, and all viewer containers.|
| `VIEWER_VNC_PORT`       | `5900`                             | VNC port inside each viewer container.                                           |

## Verification

```bash
# Ports are bound on every interface, not just localhost:
docker port guac_dispatcher
# 7081/tcp -> 0.0.0.0:7081
docker port guac_web
# 8080/tcp -> 0.0.0.0:7080

# Hit the dispatcher with the host's LAN IP and check the Location header:
HOST_IP=$(ip -4 -o route get 1.1.1.1 | awk '{print $7}')
curl -sI "http://$HOST_IP:7081/?url=https://www.africau.edu/images/default/sample.pdf" \
  | grep -i '^location:'
# location: http://<HOST_IP>:7080/guacamole/#/client/<...>?token=<...>
# Confirm NO "localhost" in there.

# Confirm viewer containers do NOT publish 5900 to the host:
docker ps --filter "label=guac-viewer=true" --format 'table {{.Names}}\t{{.Ports}}'
# (Ports column should be empty for each viewer.)
```

## Caveats / known limits

- VNC servers have **no password** (`x11vnc -nopw`). Acceptable because
  they're only reachable from inside the `guac_net` docker network; do not
  publish 5900 to the host.
- Guacamole runs with **only** the json-auth extension loaded. There is no
  user-mapping.xml, no admin user, and no other authenticator — every
  session is created by the dispatcher minting a signed json-auth blob.
  If the `JSON_SECRET_KEY` leaks, anyone can mint arbitrary connections.
  Treat that env var as a secret.
- One viewer per request: hitting `/?url=X` twice spawns two containers.
  There is no idle reaper in this prototype — `docker ps -a --filter
  label=guac-viewer=true` to see them; `GET /cleanup` on the dispatcher
  kills them all.
- VLC under Xvfb plays video without audio (no PulseAudio in the viewer
  image). That's intentional — keeps the image small.
