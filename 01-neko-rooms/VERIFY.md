# VERIFY — automated tests run

All commands below were executed on this host (linux/arm64, docker
29.4.0, compose v5.1.1) against the stack defined in
`docker-compose.yml`.

## What I verified mechanically (PASS)

1. **Dispatcher health endpoint** returns 200.
2. **neko-rooms API** is reachable and reports the expected
   `neko_images` whitelist.
3. **Classification** correctly maps extension -> kind -> image_role for
   png / jpg / pdf / html / mp3 / mp4 / unknown.
4. **`GET /?url=<url>` returns 302** with a sensible Location for all
   four test URLs (PNG, PDF, MP3, MP4).
5. **neko-rooms actually spawned a container** for each request, with
   the correct image (firefox-launch for PNG/PDF, vlc for MP3/MP4).
6. **Rooms reach state `running=true, is_ready=true`** within ~6s.
7. **Following the redirect** with `curl -L` returns 200 + the neko HTML
   page (`<title>Neko rooms</title>`).
8. **The URL was actually injected into the launcher's argv** —
   confirmed with `ps -eo args` inside both a firefox-launch and a vlc
   room container.

## What I CAN'T verify here (Manual checks needed)

This sandbox has no browser, so the **viewer rendering inside the
WebRTC stream** is the human-verifiable part. With the stack up, open
each of these in a real browser:

| Probe URL                                                                                                  | Expect inside the room                          |
|------------------------------------------------------------------------------------------------------------|-------------------------------------------------|
| `http://<host>:8081/?url=https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png` | Firefox tab showing the dice PNG full-frame.    |
| `http://<host>:8081/?url=https://www.africau.edu/images/default/sample.pdf`                              | Firefox built-in PDF viewer showing "Dummy PDF". |
| `http://<host>:8081/?url=https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3`                  | VLC playing audio. Click "Login" -> "Join" — VLC's idle visualisation should appear and audio should be audible. |
| `http://<host>:8081/?url=https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4` | VLC showing Big Buck Bunny video.               |

Neko UI quirk: you may need to click **"Login"** on the room page (no
password — it's `user_pass=""`) and then "Take control" before
keyboard/mouse work in the room. Audio/video should stream regardless
of who has control.

## Evidence dump

### Dispatcher health
```
$ curl -sS -i http://localhost:8081/healthz
HTTP/1.0 200 OK
Server: neko-rooms-dispatcher/0.2 Python/3.12.13
Content-Type: text/plain; charset=utf-8
Content-Length: 3

ok
```

### neko-rooms config (image whitelist)
```
$ curl -sS http://localhost:8080/api/config/rooms
{
  "connections":50,
  "neko_images":[
    "kasm2/neko-firefox-launch:latest",
    "ghcr.io/m1k1o/neko/firefox:latest",
    "ghcr.io/m1k1o/neko/vlc:latest",
    "ghcr.io/m1k1o/neko/chromium:latest"
  ],
  "storage_enabled":false,
  "uses_mux":true
}
```

### Classification probe (subset)
```
$ for ext in png pdf html mp3 mp4 unknownext; do
    curl -sS "http://localhost:8081/classify?url=https://x.test/y.$ext"; echo
  done
{ "url": "https://x.test/y.png",        "kind": "image", "image_role": "firefox" }
{ "url": "https://x.test/y.pdf",        "kind": "pdf",   "image_role": "firefox" }
{ "url": "https://x.test/y.html",       "kind": "html",  "image_role": "firefox" }
{ "url": "https://x.test/y.mp3",        "kind": "audio", "image_role": "vlc"     }
{ "url": "https://x.test/y.mp4",        "kind": "video", "image_role": "vlc"     }
{ "url": "https://x.test/y.unknownext", "kind": "html",  "image_role": "firefox" }
```

### Dispatch all four test URLs (each returned 302)
```
$ curl -sS -i "http://localhost:8081/?url=https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"
HTTP/1.0 302 Found
Location: http://localhost:8080/room/v-d794e05a/

$ curl -sS -i "http://localhost:8081/?url=https://www.africau.edu/images/default/sample.pdf"
HTTP/1.0 302 Found
Location: http://localhost:8080/room/v-8c91bc59/

$ curl -sS -i "http://localhost:8081/?url=https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
HTTP/1.0 302 Found
Location: http://localhost:8080/room/v-ac5bee2f/

$ curl -sS -i "http://localhost:8081/?url=https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"
HTTP/1.0 302 Found
Location: http://localhost:8080/room/v-7ef150c6/
```

### Rooms actually spawned
```
$ docker ps --filter "label=m1k1o.neko_rooms.instance" \
            --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
NAMES                   IMAGE                              STATUS
neko-rooms-v-7ef150c6   ghcr.io/m1k1o/neko/vlc:latest      Up 6 seconds (health: starting)
neko-rooms-v-ac5bee2f   ghcr.io/m1k1o/neko/vlc:latest      Up 6 seconds (health: starting)
neko-rooms-v-8c91bc59   kasm2/neko-firefox-launch:latest   Up 6 seconds (health: starting)
neko-rooms-v-d794e05a   kasm2/neko-firefox-launch:latest   Up 7 seconds (health: starting)
```

### neko-rooms API: rooms are running and ready
```
$ curl -sS http://localhost:8080/api/rooms
  -> .[] | { name, neko_image, running, is_ready, status }

{ "name": "v-7ef150c6", "neko_image": "ghcr.io/m1k1o/neko/vlc:latest",      "running": true, "is_ready": true, "status": "Up 6 seconds (health: starting)" }
{ "name": "v-ac5bee2f", "neko_image": "ghcr.io/m1k1o/neko/vlc:latest",      "running": true, "is_ready": true, "status": "Up 6 seconds (health: starting)" }
{ "name": "v-8c91bc59", "neko_image": "kasm2/neko-firefox-launch:latest",   "running": true, "is_ready": true, "status": "Up 6 seconds (health: starting)" }
{ "name": "v-d794e05a", "neko_image": "kasm2/neko-firefox-launch:latest",   "running": true, "is_ready": true, "status": "Up 7 seconds (health: starting)" }
```

### Follow-redirect returns neko HTML
```
$ for u in <four test urls>; do
    curl -sSL "http://localhost:8081/?url=$u" -o /tmp/page.html \
         -w "HTTP %{http_code} %{size_download}B  final=%{url_effective}\n"
  done
HTTP 200 7673B  final=http://localhost:8080/room/v-9171ef7c/
HTTP 200 7673B  final=http://localhost:8080/room/v-cc9d6aab/
HTTP 200 7673B  final=http://localhost:8080/room/v-528fa9c6/
HTTP 200 7673B  final=http://localhost:8080/room/v-abf534c9/

$ grep -oE '<title>[^<]+</title>' /tmp/page.html
<title>Neko rooms</title>
```

### URL env injection verified inside each room
```
$ for cid in $(docker ps --filter "label=m1k1o.neko_rooms.instance" -q); do
    name=$(docker inspect $cid --format '{{.Name}}')
    img=$(docker inspect $cid --format '{{.Config.Image}}')
    echo "$name  $img"
    docker inspect $cid --format '{{range .Config.Env}}{{println .}}{{end}}' \
      | grep -E '^(LAUNCH_URL|VLC_MEDIA)='
  done

/neko-rooms-v-7ef150c6  ghcr.io/m1k1o/neko/vlc:latest
VLC_MEDIA=https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4

/neko-rooms-v-ac5bee2f  ghcr.io/m1k1o/neko/vlc:latest
VLC_MEDIA=https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3

/neko-rooms-v-8c91bc59  kasm2/neko-firefox-launch:latest
LAUNCH_URL=https://www.africau.edu/images/default/sample.pdf

/neko-rooms-v-d794e05a  kasm2/neko-firefox-launch:latest
LAUNCH_URL=https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png
```

### Process inside each room actually got the URL on argv
```
$ docker exec <firefox-room> ps -eo args | grep firefox
/usr/bin/firefox --no-remote -P default --display=:99.0 -setDefaultBrowser \
  -width 1280 -height 720 https://www.africau.edu/images/default/sample.pdf

$ docker exec <vlc-room> ps -eo args | grep vlc
/usr/bin/vlc --x11-display=:99.0 --no-qt-privacy-ask \
  https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4
```

End-to-end proof: the URL the dispatcher receives reaches the viewer
process inside the neko room.

## Gotchas encountered & fixed during build

1. **Docker Hub `m1k1o/neko:firefox` and `m1k1o/neko:vlc` ship amd64
   only** — broken on arm64. Same images at
   `ghcr.io/m1k1o/neko/firefox` and `ghcr.io/m1k1o/neko/vlc` are
   multi-arch. The compose / dispatcher both point at ghcr.
2. **`USER neko` in our custom Firefox Dockerfile broke supervisord**:
   `Error: Can't drop privilege as nonroot user`. The upstream image
   starts as root; supervisord drops privilege per-program. Fixed by
   removing the `USER neko` line and adding a comment so we don't
   regress.
3. **`NEKO_BROWSER_HOMEPAGE` / `NEKO_DEFAULT_URL`** (which a prior draft
   of the dispatcher tried to use) **do not exist** in the neko firefox
   image. Confirmed with `grep` over the neko source tree. The custom
   image with a `LAUNCH_URL`-aware supervisord program is the working
   path.
4. **`NEKO_ROOMS_NEKO_IMAGES` is enforced** — neko-rooms rejects
   requests for any image not in this whitelist with `"invalid neko
   image"`. We list all three images we may hand out.
5. **`max_connections` must be 0 when mux mode is on** — RoomSettings
   doc warning. Set explicitly in both code paths.
6. **Stale containers from prior runs** sometimes collide on the
   `neko-rooms` / `neko-rooms-dispatcher` names. Recovery:
   `docker rm -f neko-rooms neko-rooms-dispatcher && docker compose up -d`.
   To wipe leftover rooms:
   `docker ps -aq --filter "label=m1k1o.neko_rooms.instance" | xargs -r docker rm -f`.
