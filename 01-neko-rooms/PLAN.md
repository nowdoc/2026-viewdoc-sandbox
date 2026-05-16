# PLAN — URL-to-viewer dispatcher (neko + neko-rooms)

## What was built

Three-piece docker-compose stack: a tiny **dispatcher** classifies a URL
by file extension, asks **neko-rooms** to spawn the right kind of **neko**
container with the URL pre-loaded, and 302-redirects the user into the
freshly-created room.

```
+-------------------+                                       +-------------------+
|  user             |  GET :8081/?url=...                   |   docker host     |
|  (browser)        +------+                                |                   |
|                   |      |                                |                   |
|                   |      v                                |                   |
|                   |   +--------------+   POST /api/rooms  |  +-------------+  |
|                   |   |  dispatcher  +------------------->|  |  neko-rooms |  |
|                   |   |   :8081      |                    |  |   :8080     |  |
|                   |   +------+-------+                    |  +------+------+  |
|                   |          |                            |         |         |
|                   |   302 Location:                       |         |  docker.sock
|                   |  http://host:8080/room/v-xxxxxxxx/    |         v         |
|                   |          |                            |  +-------------+  |
|                   +<---------+                            |  | room v-xxxx |  |
|                   |                                       |  |  firefox or |  |
|                   |  GET that URL  ----------------->     |  |  vlc + URL  |  |
|                   |                                       |  +-------------+  |
+-------------------+                                       +-------------------+
```

## Routing rules (classification by extension only)

| Extension                                           | neko image                          | URL is injected via       |
|-----------------------------------------------------|-------------------------------------|---------------------------|
| `png/jpg/jpeg/gif/webp/svg/bmp/ico` (image)         | `kasm2/neko-firefox-launch:latest`  | `LAUNCH_URL` env -> argv  |
| `pdf`                                               | `kasm2/neko-firefox-launch:latest`  | `LAUNCH_URL` env -> argv  |
| `html/htm` and unknown extensions                   | `kasm2/neko-firefox-launch:latest`  | `LAUNCH_URL` env -> argv  |
| `mp3/wav/ogg/flac/m4a/aac` (audio)                  | `ghcr.io/m1k1o/neko/vlc:latest`     | `VLC_MEDIA` env -> argv   |
| `mp4/mkv/webm/mov/avi` (video)                      | `ghcr.io/m1k1o/neko/vlc:latest`     | `VLC_MEDIA` env -> argv   |

## Key decisions and why

1. **No Traefik.** neko-rooms ships its own built-in HTTP reverse proxy
   for room routing under `/room/<name>/`. We set
   `NEKO_ROOMS_TRAEFIK_ENABLED=false` and expose only port 8080 for
   neko-rooms + the rooms it fronts. One less moving part.

2. **Custom `firefox-launch` image.** The stock
   `ghcr.io/m1k1o/neko/firefox` image **has no environment variable** for
   the start URL — its supervisord runs a fixed firefox command line.
   `neko-rooms`' RoomSettings doesn't expose a command override either.
   So we extend the upstream image with a tiny wrapper that appends
   `$LAUNCH_URL` to firefox's argv, and swap the supervisord program to
   call it. Built locally as `kasm2/neko-firefox-launch:latest`. Also
   flips `DisableBuiltinPDFViewer` so PDFs render in-tab.

3. **VLC stays stock.** The `ghcr.io/m1k1o/neko/vlc` image's
   supervisord runs `/usr/bin/vlc ... %(ENV_VLC_MEDIA)s` already, so we
   just set `VLC_MEDIA` via RoomSettings.envs. Zero custom build.

4. **Architecture: arm64.** The Docker Hub `m1k1o/neko:firefox` /
   `:vlc` tags ship **amd64 only** — they will not pull on this host.
   The same images at `ghcr.io/m1k1o/neko/firefox` and
   `ghcr.io/m1k1o/neko/vlc` are **multi-arch (amd64 + arm64)**, so we
   use those everywhere. The plan brief's `m1k1o/neko:vlc` form would
   have failed on this host.

5. **Image whitelist matters.** neko-rooms only spawns containers from
   images listed in `NEKO_ROOMS_NEKO_IMAGES`. Requesting an image not on
   the list returns `"invalid neko image"`. We list all three images we
   may hand out (`kasm2/neko-firefox-launch`, `vlc`, plus stock firefox
   for fallback) plus chromium for future use.

6. **Mux mode (`NEKO_ROOMS_MUX=true`).** Collapses each room's WebRTC
   media onto a single port pair (TCP+UDP) in `NEKO_ROOMS_EPR=59000-59049`.
   Without mux, every room would need its own dynamic port range —
   harder to pre-publish and firewall.

7. **`max_connections: 0`** in the dispatcher's RoomSettings — required
   when mux is on (per neko-rooms docs).

8. **Stdlib-only dispatcher.** `python3 -m http.server`-class code with
   `urllib`. No FastAPI, no `pip install` step in the Dockerfile, so
   build is ~5 seconds on a fresh host. Single file:
   `dispatcher/dispatcher.py` (~250 LOC including comments).

9. **LAN-friendly redirects.** The 302 Location host is taken from the
   incoming request's `Host` header (port stripped, neko-rooms port
   spliced in from the API response). So a request to
   `http://<PUBLIC_HOST>:8081/?url=...` redirects to
   `http://<PUBLIC_HOST>:8080/room/v-xxxx/`, not `localhost`. Override
   with `DISPATCHER_PUBLIC_HOST` env if a reverse proxy in front rewrites
   `Host`.

10. **No password.** Both images are spawned without `user_pass` or
    `admin_pass` — anyone with the room URL gets straight in.
    Acceptable for a self-hosted MVP per the brief.

## Files

```
01-neko-rooms/
  PLAN.md                 # this doc
  README.md               # quick start
  VERIFY.md               # test log
  docker-compose.yml      # neko-rooms + dispatcher + build sidecar
  .env / .env.example     # PUBLIC_HOST for LAN access
  dispatcher/
    Dockerfile            # python:3.12-slim, no pip
    dispatcher.py         # classify + neko-rooms API + 302
  firefox-launch/
    Dockerfile            # FROM ghcr.io/m1k1o/neko/firefox + LAUNCH_URL hook
```

## What this MVP does NOT do

- No HEAD/Content-Type fallback for URLs without an extension (always
  goes to firefox as the catch-all).
- No room TTL / GC — neko-rooms keeps rooms running until you call
  `DELETE /api/rooms/{id}` or `docker rm -f`.
- No auth on the dispatcher itself — anyone who can reach :8081 can
  spawn rooms (= run docker images).
- No PDF for non-`.pdf` URLs (e.g. a page that serves a PDF via
  `Content-Type: application/pdf`). Firefox would still try to render
  the page; it'll work for inline PDFs because we enabled the built-in
  viewer.
