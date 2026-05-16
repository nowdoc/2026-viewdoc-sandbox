# xpra-viewer prototype — Plan

## Goal recap

`GET http://<host>:9081/?url=<asset_url>` →
1. Classify by URL extension (pdf / image / video / audio).
2. Spawn a fresh xpra viewer container that opens the asset with the
   appropriate desktop app.
3. 302-redirect the browser to the xpra HTML5 client of that container.

No passwords, no auth, self-hosted only.

## Architecture

```
        ┌────────────────┐  GET /?url=…       ┌────────────────────┐
 browser│  http :9081    │ ─────────────────► │ dispatcher (Python)│
        │  (any host)    │ ◄───── 302 ───────  │  xpra-dispatcher   │
        └────────────────┘  Location: :PORT/   └─────────┬──────────┘
                                                          │ docker run
                                                          │ -p PORT:14500
                                                          ▼
                                            ┌────────────────────────────┐
        browser ───── HTTP/WS ────────────► │ xpra-viewer (per-asset)    │
                http://<host>:PORT/         │ Debian + xpra3 + xpdf|feh| │
                                            │ mpv  → opens TARGET_URL    │
                                            └────────────────────────────┘
```

- **One container per asset.** Stateless, ephemeral (`docker run --rm`).
- **Sibling, not child.** The dispatcher mounts `/var/run/docker.sock` and
  spawns viewer containers as siblings on the host docker engine.
- **Host-port assignment is done by the dispatcher**, not by docker's
  random ephemeral picker, so we can constrain to a small, firewallable
  range (`9082-9099`). The dispatcher reads `docker ps` to find a free
  port in that range.

## Image choices

### Base for the xpra viewer

Tried `ghcr.io/xpra-org/xpra-html5` — manifest denied (private/missing).
Tried `texastribune/xpra-html5-debian` — repo does not exist.

**Chose** `debian:bookworm-slim` + the official `xpra` apt package
(v3.1.3 in Debian 12). Decisive reasons:

- The Debian-packaged xpra **bundles the HTML5 client** at
  `/usr/share/xpra/www`, served automatically when `--html=on` and
  `--bind-tcp=…` are set. No separate `xpra-html5` package is needed (it
  exists in xpra.org's apt repo but is not in Debian's; pulling it would
  mean adding their key + repo, more moving parts for a prototype).
- Debian's xpra is current enough for HTML5 client + websocket auth=none.
- Stable, predictable build (~4 min cold).

### Viewer apps (one per kind)

| Kind  | App   | Why                                                                                                  |
|-------|-------|------------------------------------------------------------------------------------------------------|
| image | feh   | Lightweight, no toolbar/menubar noise, scales to window with `--auto-zoom`.                          |
| pdf   | xpdf  | Minimal X11 PDF viewer, starts instantly, no GTK/Qt baggage.                                         |
| video | mpv   | **Streams over HTTP natively** — important for the 250MB BigBuckBunny test asset.                    |
| audio | mpv   | Same: streams URLs, shows a tiny window with metadata. Uses `--force-window=yes`.                    |

VLC is also installed (the original brief mentioned it) but mpv was
chosen as the default for video/audio because (a) it streams URLs
without prompting, (b) starts faster, (c) doesn't fight the
"running-as-root" check that VLC enforces.

## Streaming vs downloading

The entrypoint **branches** on viewer kind:

- `feh` / `xpdf` need a local file → `curl -fsSL` to `/tmp/payload.<ext>`
  first, then launch.
- `mpv` / `vlc` are passed the **URL directly** as their argument. This
  avoids a 250MB download blocking xpra startup, and importantly avoids
  blocking the dispatcher's readiness poll past its 15s timeout.

## auth=none — disabling the password prompt

Xpra by default looks at the bind-target's auth setting and may prompt
for a token via the HTML5 client. The brief specifically requires no
auth.

**Xpra 3.1 syntax** (the one Debian ships) takes auth as separate flags,
**not** as a `,auth=none` suffix on `--bind-tcp`:

```
xpra start :100 \
    --bind-tcp=0.0.0.0:14500 \
    --tcp-auth=none \
    --ws-auth=none \   # required: HTML5 upgrades to websocket
    --html=on \
    ...
```

(Xpra 4.x/5.x changed the syntax to `--bind-tcp=…,auth=none` per the
brief's hint; that fails on 3.1 with `invalid port number:
14500,auth=none`. Documented in entrypoint.sh comments.)

## Dispatcher

`dispatcher/dispatcher.py` is a stdlib-only `ThreadingHTTPServer` (no
FastAPI / Flask dependency). Key behaviours:

- Classifies the URL by extension. Unknown → `pdf` (most "show me this
  document" cases).
- Picks a port from `9082-9099` by parsing `docker ps --format
  '{{.Ports}}'`.
- Spawns `xpra-viewer:latest` with `-p <port>:14500`, `-e TARGET_URL=…`,
  `-e VIEWER=…`, `--memory=1g --cpus=1.0`. **No bind IP on `-p`**, so
  the host port is reachable from any LAN interface.
- Polls the new container's port (via `host.docker.internal`) until it
  returns HTTP 200, up to 15s, so the 302 doesn't redirect the browser
  before xpra finishes booting.
- Returns `302 Location:
  http://<from-Host-header>:<port>/`. Using the request's `Host` header
  means a LAN client at `192.168.x.y:9081` is redirected to
  `192.168.x.y:<port>`, not `localhost:<port>`. Overridable with
  `PUBLIC_HOST`.
- Background janitor thread `docker rm -f`'s any tracked container older
  than `IDLE_TTL` (default 900s).

## Tradeoffs / things explicitly NOT done

- **No auth on viewer containers.** Per the brief. The "access token"
  is effectively the ephemeral port number being guessable.
- **Janitor reaps by age, not activity.** A user with a tab still open
  gets killed after 15 min. A v2 would track xpra session activity.
- **No HTTPS / TLS.** Plain HTTP everywhere.
- **No GPU passthrough.** The xpra X11 server uses the software renderer.
  Fine for static viewers + 720p video; would not be fine for 4K.
- **Concurrency cap implicit.** Only 18 viewer ports (9082-9099); 19th
  request 5xx's. Easy to widen.
- **No `--cap-drop=ALL`.** We tried but xpdf/feh need TTY ioctls; the
  prototype favours "it works" over hardening. Documented in code
  comments and README caveats.

## Build / run

```bash
./build.sh                 # builds xpra-viewer:latest and xpra-dispatcher:latest
docker compose up -d       # starts dispatcher on :9081
# open: http://localhost:9081/?url=<your-asset-url>
```

Cold build of `xpra-viewer:latest` is ~3m54s on this host (most of it is
apt installing vlc and its dependency tree, even though we use mpv by
default; vlc is kept available because the brief mentioned it).
Subsequent rebuilds with entrypoint edits are <1s (layer cached).
