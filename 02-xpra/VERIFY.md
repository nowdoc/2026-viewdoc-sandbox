# Verification log

Run: 2026-05-16 11:54 UTC, on the prototype host (linux/arm64, Docker 29.4.0,
Compose v5.1.1).

## Test URL substitutions

Two of the four URLs in the brief no longer serve their original payload.
We documented the failures and substituted comparable assets:

| Kind  | Original (brief)                                                                                                   | HTTP today          | Substitute used                                                                                                  |
|-------|--------------------------------------------------------------------------------------------------------------------|---------------------|------------------------------------------------------------------------------------------------------------------|
| image | `https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png`                         | 200 OK              | (unchanged)                                                                                                      |
| pdf   | `https://www.africau.edu/images/default/sample.pdf`                                                                | 301 → 404           | `https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf`                                        |
| mp3   | `https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3`                                                    | 200 OK              | (unchanged)                                                                                                      |
| mp4   | `https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4`                               | 403                 | `https://archive.org/download/BigBuckBunny_124/Content/big_buck_bunny_720p_surround.mp4`                         |

Raw probes of the originals:

```
$ curl -sIL https://www.africau.edu/images/default/sample.pdf | grep '^HTTP'
HTTP/2 301
HTTP/2 404

$ curl -sIL https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4 | grep '^HTTP'
HTTP/2 403
```

## Bring-up

```
$ ./build.sh
[build] xpra-viewer:latest        # ~3m54s cold (first build); cached after
[build] xpra-dispatcher:latest    # ~5s
[build] done

$ docker compose up -d
 Network xpra-net Created
 Container xpra-dispatcher Started

$ docker compose ps
NAME              IMAGE                    STATUS         PORTS
xpra-dispatcher   xpra-dispatcher:latest   Up 6 minutes   0.0.0.0:9081->9081/tcp, [::]:9081->9081/tcp
```

## Healthz

```
$ curl -s http://localhost:9081/healthz
{
  "ok": true,
  "viewer_image": "xpra-viewer:latest",
  "xpra_container_port": 14500,
  "viewer_port_range": "9082-9099",
  "public_host_override": null
}
```

## Test 1 — IMAGE (feh)

```
$ curl -is "http://localhost:9081/?url=https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"
HTTP/1.0 302 Found
Server: xpra-dispatcher/0.1 Python/3.12.13
Date: Sat, 16 May 2026 11:54:21 GMT
Location: http://localhost:9082/
Cache-Control: no-store
X-Viewer-Container: 7ebc8f426fed
X-Viewer-Kind: image

$ curl -sI http://localhost:9082/
HTTP/1.0 200 OK
Server: Xpra-WebSocket-Server Python/3.11.2
Content-type: text/html
```

## Test 2 — PDF (xpdf)

```
$ curl -is "http://localhost:9081/?url=https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
HTTP/1.0 302 Found
Location: http://localhost:9083/
X-Viewer-Container: cd43583ed6f3
X-Viewer-Kind: pdf

$ curl -sI http://localhost:9083/
HTTP/1.0 200 OK
Server: Xpra-WebSocket-Server Python/3.11.2
Content-type: text/html
```

## Test 3 — AUDIO (mpv streaming the mp3 URL)

```
$ curl -is "http://localhost:9081/?url=https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
HTTP/1.0 302 Found
Location: http://localhost:9084/
X-Viewer-Container: 838bce70bc53
X-Viewer-Kind: audio

$ curl -sI http://localhost:9084/
HTTP/1.0 200 OK
Server: Xpra-WebSocket-Server Python/3.11.2
Content-type: text/html
```

## Test 4 — VIDEO (mpv streaming the mp4 URL)

```
$ curl -is "http://localhost:9081/?url=https://archive.org/download/BigBuckBunny_124/Content/big_buck_bunny_720p_surround.mp4"
HTTP/1.0 302 Found
Location: http://localhost:9085/
X-Viewer-Container: f9e1bdbe1dc3
X-Viewer-Kind: video

$ curl -sI http://localhost:9085/
HTTP/1.0 200 OK
Server: Xpra-WebSocket-Server Python/3.11.2
Content-type: text/html
```

## `docker ps` snapshot (after all 4 dispatches)

```
NAMES                    PORTS                                           STATUS
xpra-viewer-17cf76e5ba   0.0.0.0:9085->14500/tcp, [::]:9085->14500/tcp   Up 5 seconds
xpra-viewer-e478ab15af   0.0.0.0:9084->14500/tcp, [::]:9084->14500/tcp   Up 5 seconds
xpra-viewer-066047d286   0.0.0.0:9083->14500/tcp, [::]:9083->14500/tcp   Up 7 seconds
xpra-viewer-d52f2b25f4   0.0.0.0:9082->14500/tcp, [::]:9082->14500/tcp   Up 8 seconds
xpra-dispatcher          0.0.0.0:9081->9081/tcp                          Up 6 minutes
```

Four ephemeral viewer containers, ports 9082-9085 inside the
9082-9099 allocation range, all status `Up`.

## Container-internal sanity (xpdf actually launched)

PDF container log tail:
```
2026-05-16 11:54:22,819 xpra GTK3 X11 version 3.1.3 64-bit
2026-05-16 11:54:22,822 connected to X11 display :100 with 24 bit colors
2026-05-16 11:54:22,928 11.7GB of system memory
```
(xpra serving HTML5, X11 :100 up, started-command xpdf is parented to xpra.)

Video container log tail (mpv streaming the URL directly):
```
VO: [sdl] 640x360 yuv420p
```
(decoder picked up the remote stream and is rendering 360p video into the
xpra X server.)

## Cleanup

```
docker ps --filter "ancestor=xpra-viewer:latest" -q | xargs -r docker stop
docker compose down
```

---

## Manual checks needed

The curl-based verification above proves:

- the dispatcher accepts the request,
- classifies by extension,
- spawns the correct viewer image,
- pins a unique host port from the 9082-9099 range,
- redirects with a syntactically correct `Location`,
- the xpra HTML5 server is listening and returns 200 / text/html on `/`,
- the per-kind viewer app launched (xpra log confirms `start-child`).

**A human still needs to confirm in a real browser:**

1. **Pixel-level rendering.** Open each `Location` in Firefox / Chrome and
   confirm that:
   - The xpra-html5 client connects (no token prompt — should be
     password-less per `--tcp-auth=none --ws-auth=none`).
   - The PNG renders correctly (transparency visible).
   - The PDF renders with xpdf's window, page 1 of dummy.pdf visible.
   - The MP3 produces an mpv window with audio meter / timeline.
     **Audio in the browser** depends on whether the xpra-html5 client
     proxies audio. We disabled pulseaudio in the container (`--no-pulseaudio`),
     so the most likely outcome is a silent mpv UI showing timeline progress
     — **but pure-audio assets cannot actually be heard** through this
     prototype. Document this as a known limit.
   - The MP4 plays video. Same caveat for sound.

2. **Interactivity.** Click / keyboard input should pass through to xpdf
   (page-down) and feh (right-arrow next image). Confirm the
   responsiveness is acceptable on the LAN.

3. **LAN reachability.** From another machine on the LAN, hit
   `http://<host-lan-ip>:9081/?url=…` and confirm the 302's `Location` host
   matches what you sent in (it should — dispatcher reuses `Host` header).

4. **Cleanup behaviour.** Leave a viewer for 15 min and confirm the
   janitor `docker rm -f`'s it (`docker logs xpra-dispatcher | grep
   janitor`).

5. **Resource budget.** Spawn a handful of viewers and watch `docker stats`
   — each container is capped to 1 CPU / 1 GB RAM by the dispatcher, but
   real consumption with mpv decoding 720p video is worth eyeballing.
