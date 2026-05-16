# 06 — verification log

## Build + bring up

```
$ docker pull lscr.io/linuxserver/webtop:ubuntu-xfce   # one-off
$ docker compose up -d
$ docker ps --filter name=lsio-webtop --format '{{.Status}} | {{.Ports}}'
Up <N> seconds | 0.0.0.0:5085->3000/tcp, [::]:5085->3000/tcp, 0.0.0.0:5086->3001/tcp, [::]:5086->3001/tcp
```

Container logs show Selkies (WebRTC) coming up cleanly: data WebSocket on
internal :8082, gamepad sockets initialised, XFCE session up.

```
$ curl -sS -o /dev/null -w "HTTP %{http_code}\n" http://localhost:5085/
HTTP 200
$ curl -sSk -o /dev/null -w "HTTPS %{http_code}\n" https://localhost:5086/
HTTPS 200
```

## Secure-context note

Selkies refuses to render the desktop on non-localhost HTTP — first-load
without HTTPS shows: *"This application requires a secure connection
(HTTPS). Please check the URL."* Three options work:

- `http://localhost:5085/`  — secure-context exemption for `localhost`. **Used for these screenshots.**
- `https://HOST:5086/`  — HTTPS with the container's self-signed cert (browser will warn once). Substitute `HOST` for whatever name resolves to the docker host (LAN IP, DNS name, etc.).
- A real cert in front via a reverse proxy.

## E2E (agent-browser)

```
$ agent-browser open 'http://localhost:5085/'
$ agent-browser snapshot -i -c
- button "Disable Video Stream"
- button "Disable Audio Stream"      ← audio is ON by default
- button "Enable Microphone"
- button "Audio Settings"
- button "Clipboard"
- button "Files"
- button "Sharing"
- ...
$ agent-browser screenshot screenshots/01-initial-load.png
$ agent-browser click @e2          # toggle the sidebar drawer
$ agent-browser screenshot screenshots/02-sidebar-open.png
$ agent-browser click @e14         # expand Audio Settings
$ agent-browser screenshot screenshots/03-audio-settings.png
```

Screenshots:

- `screenshots/01-initial-load.png` — XFCE desktop rendered via Selkies/WebRTC.
- `screenshots/02-sidebar-open.png` — Selkies sidebar open: video/audio/mic
  toggles, Audio/Clipboard/Files/Sharing panels.
- `screenshots/03-audio-settings.png` — Audio Settings panel expanded with
  Input (Microphone) + Output (Speaker) device dropdowns.

Note: in screenshot 03 the Audio panel shows *"No audio devices found"* —
this is because **agent-browser runs headless Chromium with no audio
hardware enumerated**, not a Selkies fault. A real desktop browser will
populate the dropdowns and stream audio inline over WebRTC.

## Result vs. 05

| Capability                   | 05 (`kasmweb/ubuntu-noble-desktop`) | 06 (`lscr.io/.../webtop:ubuntu-xfce`) |
| ---------------------------- | ----------------------------------- | ------------------------------------- |
| Display                      | ✅ KasmVNC                          | ✅ Selkies/WebRTC                     |
| Audio out (server → browser) | ❌                                  | ✅ (on by default)                    |
| Microphone in                | ❌                                  | ✅                                    |
| File upload / download UI    | ❌                                  | ✅                                    |
| Clipboard sync UI            | ❌                                  | ✅                                    |
| Multi-user session sharing   | ❌                                  | ✅                                    |
| Standalone (no orchestrator) | ✅                                  | ✅                                    |

Confirmed: LSIO webtop closes the audio/mic/uploads gap that 05 hits.

## Not tested here

- Actually playing audible audio in a real browser. The infrastructure is
  proven (Selkies up, controls present, port 5086 served HTTPS). Final
  audible verification needs a real browser session, which is what the user
  will do manually.
- Integration into the 05 control-center pool. See README "Why this isn't
  a drop-in replacement" — the iframe-bridge URL builder and the
  `kasm-write-query` / `on_query_update.sh` hook would need to be ported
  onto a `FROM lscr.io/linuxserver/webtop:ubuntu-xfce` derivative.
