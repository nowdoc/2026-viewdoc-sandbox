# 04-kasm-standalone — plan & decisions

## Architecture

```
GET http://HOST:6081/?url=<asset>
   │
   ▼
 dispatcher (FastAPI-shaped stdlib HTTP server)
   ├─ classify by extension against mapping.yaml
   ├─ pick a free host port in 6082–6099
   ├─ docker run -d --rm -p <port>:6901 \
   │      -e VNC_PW=<random-hex> \
   │      -e LAUNCH_URL=<url>     (chromium)  OR
   │      -e APP_ARGS=<url>       (vlc)
   │      <kasm2/{chromium,vlc}-notls:latest>
   ├─ wait until http://host.docker.internal:<port>/ returns 200/30x
   ▼
 302 Location: http://<request-host>:<port>/?password=<pw>&autoconnect=1&resize=remote
```

No control plane, no central KASM Workspaces, no Postgres. Just docker.sock-mounted dispatcher + ephemeral kasmweb containers.

## Image choices

- `kasmweb/chromium:1.16.0` (not `kasmweb/chrome` — chrome is amd64-only at 1.16.0, chromium is multi-arch and works on this arm64 host).
- `kasmweb/vlc:1.16.0` for audio + video.

Both wrapped in a tiny `images/Dockerfile.{chromium,vlc}` that produces `kasm2/{chromium,vlc}-notls:latest`. The wrappers exist to disable TLS — see next section.

## Disabling HTTPS (the hard part)

The brief suggested `KASM_SVC_HTTPS=disabled`. **That env var is not honored on `kasmweb/chromium:1.16.0` or `kasmweb/vlc:1.16.0`** — the string doesn't appear anywhere in `/dockerstartup/`, `/opt/`, or the KasmVNC binaries. Verified by `docker exec ... grep -r KASM_SVC_HTTPS`.

What actually works (the wrapper images do both):

1. **Edit `/dockerstartup/vnc_startup.sh`** at image build time:
   ```dockerfile
   RUN sed -i 's/-sslOnly /-DisableBasicAuth /g' /dockerstartup/vnc_startup.sh
   ```
   - removes the `-sslOnly` flag that forces TLS
   - simultaneously adds `-DisableBasicAuth` (which lives in the same argv slot) so HTTP basic auth on `/` is also turned off — otherwise the browser shows a credential prompt before NoVNC can read `?password=`/`autoconnect=1` from the URL
2. **Override `/etc/kasmvnc/kasmvnc.yaml`** with `network.ssl.require_ssl: false`. The default yaml has `require_ssl: true`, which wins over the CLI alone; both have to be off for plain HTTP to be served.

After both: `curl http://localhost:<port>/` returns `200` with the NoVNC `index.html`; no TLS handshake, no basic-auth prompt.

## Auto-login

NoVNC reads `?password=<pw>&autoconnect=1` and submits without user interaction. We mint a random 16-hex password per session in the dispatcher and set both the container's `VNC_PW` env AND the redirect URL's query param.

`?resize=remote` is also appended so the container's Xvnc resizes to fit the browser window. Documented at https://www.kasmweb.com/kasmvnc/docs/master/parameters.html.

## Network accessibility

Matches the convention in `02-xpra`/`03-guacamole`:
- Dispatcher published on `0.0.0.0:6081` (no `127.0.0.1` bind).
- Viewer containers spawned with `-p <port>:6901` (no bind IP) → docker binds on all interfaces.
- Redirect Location uses the request's `Host` header (port stripped). Honors `PUBLIC_HOST` env override.

## Gotchas encountered

- **`docker.io` apt package** on Debian 13 (trixie) ships only `docker-init`. The dispatcher's Dockerfile downloads the official static docker CLI binary instead.
- **VLC first-run "Privacy and Network Access Policy" dialog** blocks playback until clicked. Seeded `/home/kasm-default-profile/.config/vlc/vlcrc` with `qt-privacy-ask=0`. Writing to `/home/kasm-user/.config` directly breaks the image's `kasm_default_profile.sh` (it copies the default profile at startup and can't overwrite a pre-existing dir).
- **`HEAD /` on the viewer returns 404** even though `GET /` returns 200. The dispatcher's wait-ready loop uses raw socket GET, not curl-HEAD, so this isn't actually a problem.
- **`KASM_SVC_AUDIO=disabled`** turns off the audio websocket and the upload/download services to cut cold-start by a few seconds. VLC then logs a single non-fatal "audio failed" error popup once — accepted as v1 trade-off; the video still plays.

## Phase 2 — LibreOffice + Ubuntu desktop

Added after the v1 build landed.

### `kasm2/libreoffice-notls` (Office docs)

LibreOffice can't open HTTP(S) URLs directly — needs a file path. The wrapper:

1. Same `sed -sslOnly` + `kasmvnc.yaml` patch as the other wrappers.
2. Replaces `/dockerstartup/custom_startup.sh` with a version that:
   - Reads `$LAUNCH_URL` or `$KASM_URL`.
   - If the value is `http(s)://…`, `curl -fsSL` downloads it to `/tmp/payload.<ext>` (extension inferred from the URL path, falling back to `bin`).
   - Re-exports `$URL` to that local path.
   - Runs the same supervisor loop as the upstream script.
3. Seeds `/home/kasm-default-profile/.config/libreoffice/4/user/registrymodifications.xcu` with `FirstRun=false`, `ShowTipOfTheDay=false`, and a non-zero `ooSetupLastVersion` so the post-install welcome assistant doesn't pop.

### `kasm2/ubuntu-notls` (full desktop, optionally with a URL)

- TLS-disable patch (same as the others).
- `ubuntu-custom-startup.sh` copied to `/dockerstartup/custom_startup.sh` — runs as a backgrounded child of `vnc_startup.sh`. If `LAUNCH_URL` (or `KASM_URL`) is set, downloads the asset to `/tmp/payload.<ext>` after `desktop_ready` and opens it. If unset, the script no-ops and you get a plain desktop.
- Opener is type-aware (not raw `xdg-open`): PDF/HTML/images → Firefox (built-in PDF.js viewer + native image rendering), Office docs → LibreOffice, audio/video → VLC, anything else → `xdg-open`. Avoids Ubuntu's default which sends PDFs to GIMP because GIMP registers as a PDF importer.

So `/?desktop=ubuntu` gives a plain desktop and `/?desktop=ubuntu&url=<file>` gives the desktop with the file pre-opened in the right app.

## Query-param passthrough

The dispatcher reserves three query keys for itself: `desktop`, `url`, `u`. Anything else on the inbound URL gets forwarded to the spawned container as env vars:

- `KASM_Q_<UPPERCASE_KEY>=<value>` per param (after key sanitisation)
- `KASM_QUERY` = full filtered query string, URL-encoded
- `KASM_QUERY_KEYS` = space-separated list of forwarded key names

The `ubuntu-custom-startup.sh` script additionally writes these to `/tmp/kasm_query.env` (shell-sourceable) and `/tmp/kasm_query.json` (parsed) so apps inside the desktop can pick them up without needing to inherit env from the kasm session root.

Defensive limits: max 32 forwarded keys, each value capped at 4096 bytes; keys must match `^[A-Za-z][A-Za-z0-9_]{0,63}$`.

Use case: pass user-supplied metadata (theme, locale, auth tokens, dataset IDs) from the browser URL into apps running inside the kasm desktop. Works on every prototype path — `/?url=...&foo=bar` and `/?desktop=ubuntu&foo=bar` both surface `KASM_Q_FOO=bar` inside.

## Audio

Initially disabled (`KASM_SVC_AUDIO=disabled`) for faster cold-start, then re-enabled — the side-effect was that VLC also showed a recurring "audio decoder failed" error popup because there was no PulseAudio sink to write to. Re-enabling kicks the kasmweb-shipped audio pipeline back on:

- `pulseaudio --start` per-session daemon.
- `ffmpeg -f pulse … -f mpegts http://127.0.0.1:8081/kasmaudio` captures and pipes to the internal audio bus.
- `kasm_audio_out-linux` serves the audio over websocket on port 4901 (TLS internally, multiplexed onto the public 6901 by KasmVNC's main server).

VLC plays cleanly (no error popup), and audio reaches the browser. KasmVNC's NoVNC client auto-subscribes to the audio stream after `?autoconnect=1`.

### Dispatcher `/?desktop=<name>`

Added a parallel path to `/?url=`. The dispatcher looks up rules whose `kind == "desktop"` and whose `extensions` list contains the requested name; the rest of the spawn / port-allocation / redirect logic is identical.

## Out of scope (future)

- LinuxServer `baseimage-kasmvnc` for exotic file types — same `sed`+yaml trick should apply, deferred until a concrete need.
- Per-session TTL / sweeper — current behaviour leaves containers running until `docker rm -f` (containers are `--rm` so they go away on stop).
- Caddy/Traefik TLS in front — would let us terminate TLS at the edge while keeping container-internal plain HTTP.
- HTTPS HEAD support on the dispatcher (returns 405 today; not actually required for any client).
- More desktop variants (`kasmweb/core-ubuntu-noble`, `kasmweb/kali-rolling-desktop`) — same Dockerfile template as `Dockerfile.ubuntu`, mapped under additional names in `mapping.yaml`.
