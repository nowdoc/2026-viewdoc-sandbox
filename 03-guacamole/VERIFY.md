# VERIFY — Guacamole URL Dispatcher (prototype 03)

Run on 2026-05-16. amd64 Guacamole images running on an arm64 host via
orbstack qemu emulation — works, but the Tomcat WAR deploy can take
25-90 s on the first start; subsequent restarts may stall and need an
explicit `docker restart guac_web` if `up -d --build` is run again
while Guacamole is busy.

## 1. `docker compose ps`

```
NAME              IMAGE                       SERVICE      STATUS                   PORTS
guac_dispatcher   guac-dispatcher:latest      dispatcher   Up 7 minutes             0.0.0.0:7081->7081/tcp
guac_guacd        guacamole/guacd:1.5.5       guacd        Up 7 minutes (healthy)   4822/tcp
guac_web          guacamole/guacamole:1.5.5   guacamole    Up 44 seconds            0.0.0.0:7080->8080/tcp
```

## 2. Guacamole webapp reachable

```
$ curl -sI http://localhost:7080/guacamole/
HTTP/1.1 200
Cache-Control: no-cache
Content-Type: text/html
Content-Length: 2811
```

## 3. Dispatcher health

```
$ curl -s http://localhost:7081/healthz
{"ok":true}
```

## 4. Dispatch verification (all four test URLs)

Each `curl` returns a 302 to a Guacamole client URL with a one-shot
json-auth token in the query string. The base64-encoded URL fragment
encodes the dynamically-created connection identifier; it decodes to
`viewer-<id>\0c\0json` where `json` is the json-auth datasource.

### PDF

```
$ curl -s -D - -o /dev/null \
    'http://localhost:7081/?url=https://www.africau.edu/images/default/sample.pdf'
HTTP/1.1 302 Found
location: http://localhost:7080/guacamole/#/client/dmlld2VyLWMzNjUyYWUzAGMAanNvbg?token=C38D7EAF7E82F8329C8AFA4CC86D6F062B0E5001E2D3F65B4BD702B041432532
```

### PNG

```
$ curl -s -D - -o /dev/null \
    'http://localhost:7081/?url=https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png'
HTTP/1.1 302 Found
location: http://localhost:7080/guacamole/#/client/dmlld2VyLWE5ZTE5MjU1AGMAanNvbg?token=384F77CBA5ED71BB4E410110CC3372188B01CC1B4FCA591AA2468990EEB4C035
```

### MP3

```
$ curl -s -D - -o /dev/null \
    'http://localhost:7081/?url=https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3'
HTTP/1.1 302 Found
location: http://localhost:7080/guacamole/#/client/dmlld2VyLWJiNjIwZGM1AGMAanNvbg?token=BF8D96577B76B2218C41FB1EC1EA28DD96527454A52FB598942ED1719BF9C289
```

### MP4

```
$ curl -s -D - -o /dev/null \
    'http://localhost:7081/?url=https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4'
HTTP/1.1 302 Found
location: http://localhost:7080/guacamole/#/client/dmlld2VyLWNmMDkyMzNiAGMAanNvbg?token=0A3295938D66E49CE4E9BE4BC5A47A08BA6D3A61958B582B7ADA0538A2473107
```

## 5. Viewer containers spawn per request

```
$ docker ps --filter "label=guac-viewer=true" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
NAMES                  IMAGE                STATUS
guac-viewer-7f6314e3   guac-viewer:latest   Up 7 seconds
```

Containers are `--rm` so they self-clean when killed. Several burned through
during the test (xpdf/feh sometimes exit when EOF on stdin or window-manager-
free Xvfb confuses them); the dispatcher then loses the VNC backend and
Guacamole shows a disconnect. For the visual MVP this is acceptable — see
"known follow-ups" in PLAN.md.

## 6. Dispatcher log excerpts (end-to-end flow)

```
dispatch url=...sample.pdf viewer=xpdf ext=pdf
spawning viewer guac-viewer-61d14c47 viewer=xpdf ext=pdf url=...sample.pdf
viewer guac-viewer-61d14c47 ready: vnc=guac-viewer-61d14c47:5900 (docker-internal)
HTTP Request: POST http://guacamole:8080/guacamole/api/tokens "HTTP/1.1 200 "
redirect -> http://localhost:7080/guacamole/#/client/...?token=... (conn=viewer-c3652ae3, ...)
```

Step-by-step:
1. classify URL by extension
2. `docker run` the viewer image, attached to `guac_net` (no host port mapping)
3. `wait_for_tcp(<container_name>, 5900)` — docker embedded DNS resolves the
   container by name; both the dispatcher and `guacd` are on the same network.
4. mint AES-128-CBC + HMAC-SHA256 json-auth blob → POST `/api/tokens` → token
5. 302 with `#/client/<base64>?token=<token>` so the SPA auto-opens
   the connection without showing a login screen.

## 7. Network accessibility (LAN reachability)

This prototype must be reachable from other machines on the LAN, not just
`localhost`. Three independent checks:

### 7a. Both host ports bind on `0.0.0.0`

```
$ docker port guac_dispatcher
7081/tcp -> 0.0.0.0:7081
$ docker port guac_web
8080/tcp -> 0.0.0.0:7080
$ docker port guac_guacd
(empty — guacd has no host port mapping)
```

Confirmed in `ss -tln`:

```
LISTEN 0  4096   0.0.0.0:7080  0.0.0.0:*
LISTEN 0  4096   0.0.0.0:7081  0.0.0.0:*
```

### 7b. 302 `Location` uses the request's Host header, not `localhost`

Hitting the dispatcher on the real LAN IP:

```
$ HOST_IP=$(ip -4 -o route get 1.1.1.1 | awk '{print $7}')
$ echo $HOST_IP
<LAN-IP>
$ curl -s -o /dev/null -D - "http://$HOST_IP:7081/?url=https://www.africau.edu/images/default/sample.pdf" \
    | grep -iE '^(HTTP|location:)'
HTTP/1.1 302 Found
location: http://<LAN-IP>:7080/guacamole/#/client/dmlld2VyLTQwN2ExN2FlAGMAanNvbg?token=BD26085A03EDD8BE6E891D00CEB02D7CD0C8F9A871716F0C5E14980EE4F7E6C1
```

The `Location` echoes the LAN IP. No `localhost` anywhere.

Spoofed Host header (to prove the dispatcher really tracks it):

```
$ curl -s -o /dev/null -D - "http://127.0.0.1:7081/?url=...pdf" -H "Host: viewer.example.com:7081" \
    | grep -i location
location: http://viewer.example.com:7080/guacamole/#/client/...
```

### 7c. `PUBLIC_HOST` env var overrides the Host header

With `PUBLIC_HOST=guac.example.org` on a test dispatcher container:

```
$ curl -s -o /dev/null -D - "http://127.0.0.1:7082/?url=...pdf" -H "Host: random-host:7082" \
    | grep -i location
location: http://guac.example.org:7080/guacamole/#/client/...
```

### 7d. Viewer VNC ports stay internal

```
$ docker ps --filter "label=guac-viewer=true" --format 'table {{.Names}}\t{{.Ports}}'
NAMES                  PORTS
guac-viewer-7f6314e3   5900/tcp

$ docker inspect guac-viewer-7f6314e3 --format '{{json .NetworkSettings.Ports}}'
{"5900/tcp": null}

$ ss -tln | grep -E ':5900\b' || echo "no host bindings"
no host bindings
```

The `5900/tcp` in the `docker ps` Ports column is just the EXPOSE — there
is no `0.0.0.0:NNNN->5900/tcp` host mapping, so VNC is unreachable from
the LAN. guacd reaches it by container name on the `guac_net` bridge.

### 7e. Firewall

The host firewall must allow inbound `7080/tcp` (Guacamole web) and
`7081/tcp` (dispatcher). Viewer 5900 is internal — no firewall rule
needed. See README.md for distro-specific commands.

## Manual checks still needed (browser-based)

These cannot be done from headless `curl`; they require a browser to render
the Guacamole HTML5 canvas client with the framebuffer received from guacd.

- [ ] Open the PDF dispatch URL in a browser. The Guacamole client tab should
      open and, inside the canvas, render `xpdf` showing page 1 of
      `sample.pdf` (fullscreen).
- [ ] Open the PNG dispatch URL. Expect `feh` showing the transparent-PNG
      checkerboard demo at fullscreen.
- [ ] Open the MP3 dispatch URL. Expect the `vlc` GUI with the audio
      progressing on the seek bar. (Audio playback through the browser is
      NOT wired — vlc plays under Xvfb without PulseAudio bridge.)
- [ ] Open the MP4 dispatch URL. Expect `vlc` rendering BigBuckBunny visually
      inside the noVNC canvas.
- [ ] Confirm there is NO Guacamole login page in the flow — the token in the
      query string must pre-authenticate the SPA.

## Tear-down

```bash
cd /srv/kasm2/03-guacamole
docker compose down
# Stop any orphan viewers (defensive — they should --rm themselves)
docker rm -f $(docker ps -aq --filter "label=guac-viewer=true") 2>/dev/null || true
docker network rm guac_net 2>/dev/null || true
```
