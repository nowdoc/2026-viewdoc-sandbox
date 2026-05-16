# 01-neko-rooms — URL-to-viewer dispatcher (neko + neko-rooms)

Self-hosted, password-less ephemeral "viewer rooms". Hit the dispatcher
with `?url=<some-url>` and you get redirected into a fresh neko room
that's already opened the URL in either Firefox (for HTML / images /
PDF) or VLC (for audio / video).

See `PLAN.md` for the architecture diagram and design notes,
`VERIFY.md` for the test log.

## Layout

```
01-neko-rooms/
  docker-compose.yml       # neko-rooms + dispatcher + firefox-launch build sidecar
  .env / .env.example      # set PUBLIC_HOST to this host's LAN IP / hostname
  dispatcher/
    Dockerfile             # python:3.12-slim, stdlib-only
    dispatcher.py          # classify by ext, call neko-rooms, 302
  firefox-launch/
    Dockerfile             # FROM ghcr.io/m1k1o/neko/firefox + LAUNCH_URL hook
  PLAN.md                  # architecture + decisions
  VERIFY.md                # test results + manual-check checklist
```

## Quick start

```bash
cd 01-neko-rooms
cp .env.example .env
# Edit .env: set PUBLIC_HOST to this host's LAN-reachable IP/hostname.
# The example ships blank — `docker compose up` will fail until you set it.

# First-time build of the custom firefox image (~2GB pull):
docker compose --profile build build firefox-launch-build

# Up the stack:
docker compose up -d

# Sanity check:
curl -sS http://localhost:8081/healthz             # -> ok
curl -sS http://localhost:8080/api/config/rooms    # -> JSON, image whitelist
```

## Try it

```bash
# Image -> Firefox
curl -i "http://localhost:8081/?url=https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png" | head

# PDF -> Firefox (built-in viewer)
curl -i "http://localhost:8081/?url=https://www.africau.edu/images/default/sample.pdf" | head

# MP3 -> VLC
curl -i "http://localhost:8081/?url=https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3" | head

# MP4 -> VLC
curl -i "http://localhost:8081/?url=https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4" | head
```

Each returns HTTP 302 to `http://<host>:8080/room/v-xxxxxxxx/`. Open
that URL in a browser to see the room. On the neko UI: click "Login"
(no password) then "Take control" if you need to interact.

## How the routing works

The dispatcher classifies the URL by file extension only (no HEAD lookup).

| Extension                          | -> kind  | -> Image                            | URL passed via |
|------------------------------------|----------|-------------------------------------|----------------|
| png/jpg/jpeg/gif/webp/svg/bmp/ico  | image    | `kasm2/neko-firefox-launch:latest`  | `LAUNCH_URL`   |
| pdf                                | pdf      | `kasm2/neko-firefox-launch:latest`  | `LAUNCH_URL`   |
| html/htm + unknown                 | html     | `kasm2/neko-firefox-launch:latest`  | `LAUNCH_URL`   |
| mp3/wav/ogg/flac/m4a/aac           | audio    | `ghcr.io/m1k1o/neko/vlc:latest`     | `VLC_MEDIA`    |
| mp4/mkv/webm/mov/avi               | video    | `ghcr.io/m1k1o/neko/vlc:latest`     | `VLC_MEDIA`    |

The dispatcher also exposes `GET /classify?url=...` for debugging the
routing without spawning a room.

## LAN-reachable by design

Every published port binds `0.0.0.0`. The 302 `Location` host is taken
from the incoming request's `Host` header (port stripped, replaced with
neko-rooms' port from the API response), so a request to
`http://<PUBLIC_HOST>:8081/?url=...` redirects back to
`http://<PUBLIC_HOST>:8080/...` — never to `localhost`.

| Env var                     | Where           | Purpose                                                                                   |
|-----------------------------|-----------------|-------------------------------------------------------------------------------------------|
| `PUBLIC_HOST`               | `.env`          | LAN/internet-reachable host of this docker host. Used for `NEKO_ROOMS_INSTANCE_URL` and `NEKO_ROOMS_NAT1TO1`. |
| `DISPATCHER_PUBLIC_HOST`    | `.env` (opt)    | Force the dispatcher's 302 Location host instead of using the request's `Host` header. Useful behind a proxy that rewrites Host. |
| `FIREFOX_IMAGE`             | dispatcher      | Image used for image/pdf/html kinds (default `kasm2/neko-firefox-launch:latest`).         |
| `VLC_IMAGE`                 | dispatcher      | Image used for audio/video kinds (default `ghcr.io/m1k1o/neko/vlc:latest`).               |
| `NEKO_ROOMS_NEKO_IMAGES`    | neko-rooms      | Whitelist of images neko-rooms will spawn. **Both images above must be on it.**           |
| `NEKO_ROOMS_EPR`            | neko-rooms      | WebRTC mux port range (default `59000-59049`).                                            |

## Firewall

Confirm the host's firewall allows inbound traffic on the relevant
ports from your LAN:

- **TCP 8080** — neko-rooms API + neko room HTTP (where 302 redirects you).
- **TCP 8081** — dispatcher.
- **TCP+UDP 59000-59049** — WebRTC mux range. Without these the room
  page loads but the video/audio stream never connects.

## Tearing down

```bash
docker compose down
# Room containers spawned by neko-rooms aren't part of this compose
# project — delete leftovers if needed:
docker ps -aq --filter "label=m1k1o.neko_rooms.instance" | xargs -r docker rm -f
```

## Architecture notes & known caveats

- **arm64 host?** This stack uses `ghcr.io/m1k1o/neko/*` images, which
  are multi-arch. The Docker Hub `m1k1o/neko:*` tags are amd64-only and
  will fail on arm64.
- **Custom firefox image:** The upstream
  `ghcr.io/m1k1o/neko/firefox` has no env var for a start URL, so we
  build `kasm2/neko-firefox-launch:latest` on top of it with a
  supervisord program that respects `$LAUNCH_URL`. Also flips
  `DisableBuiltinPDFViewer` so PDFs render in-tab. See `PLAN.md`.
- **No auth.** Anyone reaching `:8081` can spawn neko containers on
  this host. Don't expose to the public internet.
- **No room TTL/GC.** Spawned rooms persist until you delete them. See
  the "Tearing down" snippet above for cleanup.
