# URL-to-Viewer dispatcher: three prototypes compared

All three MVPs accept `GET /?url=<asset-url>`, classify by extension, spawn an ephemeral container with the asset injected, and 302-redirect the browser into a viewer session. **No password prompt anywhere.** Bound on `0.0.0.0`, reachable from the LAN.

Validated end-to-end with the `agent-browser` CLI — screenshots in each prototype's `screenshots/` folder.

## Per-prototype results

### 01 neko-rooms (`:8081` dispatcher → `:8080` neko-rooms)

| | |
|---|---|
| Transport | **WebRTC** (lowest latency, best for video/audio) |
| Spawner | `neko-rooms` REST API (existing, no custom code needed) |
| Viewer image (browser-class) | Custom `kasm2/neko-firefox-launch` (wraps `ghcr.io/m1k1o/neko/firefox`) — reads `LAUNCH_URL` env, strips force-installed extensions |
| Viewer image (media) | Stock `ghcr.io/m1k1o/neko/vlc` — reads `VLC_MEDIA` env |
| Auto-login | `?usr=guest&pwd=guest` query params on redirect → neko frontend auto-submits |
| End-to-end validated | PDF (`xpdf-clean.png`), MP4 (`neko-mp4-vlc.png`) |
| Visual quality | **Best** — true browser PDF viewer (PDF.js), smooth video playback |
| Boot time | ~10 s for room to be healthy |

### 02 xpra (`:9081` dispatcher → ephemeral viewers on `9082-9099`)

| | |
|---|---|
| Transport | HTML5 over WebSocket (older protocol than neko's WebRTC) |
| Spawner | Custom Python `docker run` per request — dispatcher mounts docker.sock |
| Viewer image | Single `xpra-viewer:latest` (Debian + xpra 3.1.3 + xpdf/feh/vlc/mpv) |
| Auto-login | None required — xpra `--tcp-auth=none --ws-auth=none` |
| End-to-end validated | PDF via xpdf (`xpra-pdf-final.png`) |
| Visual quality | Lower fidelity — raw X11 app windows, native widget look |
| Boot time | ~15 s (container + xpra session init) |

**Issues hit during validation (fixed):**
- Debian-packaged xpra-html5 ships broken symlinks; `libjs-jquery libjs-jquery-ui` not in deps → had to add manually.
- xpra server crashes on first WebSocket handshake with `No module named 'PIL'` → had to add `python3-pil`.
- xpra v3.1.3's HTML5 client doesn't expose a stable URL-param auto-connect for non-trivial cases — works because we're hitting the bound port directly with no auth, but the UX is bare-bones (no toolbar polish).

### 03 guacamole (`:7081` dispatcher → `:7080` Guacamole web)

| | |
|---|---|
| Transport | HTML5 canvas over guacd → VNC (RFB) |
| Spawner | Custom Python `docker run` + Guacamole **json-auth** token mint per request |
| Viewer image | Custom `guac-viewer` (Debian + Xvfb + x11vnc + xpdf/feh/vlc) |
| Auto-login | Dispatcher mints AES-128-CBC + HMAC-SHA256 signed blob describing the VNC connection → exchanges at `/api/tokens` for a session token → redirect URL embeds the token |
| End-to-end validated | PDF via xpdf (`guac-pdf.png`) |
| Visual quality | Plain VNC fidelity, no audio support out of the box |
| Boot time | ~15–30 s (Tomcat WAR cold start on first request; ~10 s steady-state) |

**Notable architecture pivot during build:** the obvious `user-mapping.xml` + REST `POST /api/session/data/.../connections` path returns `403 PERMISSION_DENIED` for file-auth users. The dispatcher uses the `guacamole-auth-json` extension instead — the canonical Guacamole pattern for ephemeral, anonymous connections. No DB, no admin user.

## Side-by-side scorecard

| Dimension | neko-rooms | xpra | guacamole | kasm-standalone |
|---|---:|---:|---:|---:|
| Lines of custom code | low (uses neko-rooms API) | medium (DIY spawner) | medium (DIY spawner + crypto) | medium (DIY spawner + image patching) |
| Components to run | 2 (rooms + dispatcher) | 1 (dispatcher) + ephemeral viewers | 3 (guacd + Tomcat + dispatcher) + ephemeral viewers | 1 (dispatcher) + ephemeral viewers |
| Latency / responsiveness | **★★★** (WebRTC) | ★★ (WebSocket) | ★★ (canvas) | **★★★** (KasmVNC — JPEG/QOI, very good) |
| Video/audio quality | **★★★** (native codecs) | ★ (no audio configured) | ★ (no audio) | ★★ (video great; audio disabled in v1) |
| PDF rendering quality | **★★★** (Firefox PDF.js) | ★★ (xpdf — bare) | ★★ (xpdf — bare) | **★★★** (Chromium built-in viewer) |
| Viewer image catalog | rich (neko-apps) — VLC, Firefox, Chrome, Tor, etc. | DIY | DIY | **richest** — full `kasmweb/*` catalog + LinuxServer images |
| Out-of-box "no-auth" support | needs URL-param trick | yes (`--*-auth=none`) | needs json-auth extension | needs `sed`+yaml patch in wrapper image |
| Memory per session | ~700 MB (full browser) | ~150 MB (xpdf), ~250 MB (vlc) | ~150 MB (xpdf), ~250 MB (vlc) | ~600 MB (chromium), ~400 MB (vlc) |
| Cold-start latency | ~10 s | ~15 s | ~15 s steady | ~25-30 s (chromium-heavy bootstrap) |
| Operational complexity | medium | low | high (Tomcat + JCE + extensions) | low-medium (one wrapper Dockerfile per image) |
| Best for | mixed-file dispatcher with media | minimum-deps prototyping | enterprise with existing Guacamole | mixed-file dispatcher needing Office/CAD/etc. (next phase) |

## Recommendation

**Pick `neko-rooms` as the primary dispatcher** — best UX (WebRTC, near-native video), smallest amount of custom orchestration code (neko-rooms's REST API does the spawning), and proven on a real codebase (`kasmweb/*` is internally similar). Real visual win for video + audio.

**Use `kasm-standalone` for the long tail** — file types neko-apps doesn't cover (Office, CAD, image editors, niche browsers). The `kasmweb/*` ecosystem is the richest open library of containerised desktop apps. Same network-accessibility shape, no central server required. Pair these two and you cover ~everything.

**xpra is the minimum-deps fallback** — single dispatcher container, no extra orchestrator, but the visual fidelity and audio story are weaker, and the Debian xpra-html5 packaging has rough edges (broken JS symlinks, missing PIL dep — both fixed in our image).

**Guacamole is the worst fit for this specific use case** — three persistent services for what amounts to one redirect-per-request. Worth it only if you already run Guacamole for other reasons and want to consolidate.

## Files per prototype

- `01-neko-rooms/{PLAN.md, README.md, VERIFY.md, docker-compose.yml, dispatcher/, firefox-launch/, screenshots/}`
- `02-xpra/{PLAN.md, README.md, VERIFY.md, docker-compose.yml, dispatcher/, images/, screenshots/}`
- `03-guacamole/{PLAN.md, README.md, VERIFY.md, docker-compose.yml, dispatcher/, images/, screenshots/}`
- `04-kasm-standalone/{PLAN.md, README.md, VERIFY.md, docker-compose.yml, .env, dispatcher/, images/, screenshots/}`

## Things still owed to a production roll-out

- Session sweeper / TTL (all three currently leave containers running indefinitely).
- Resource caps per session (`--memory --cpus`).
- Concurrency limit on the dispatcher.
- HTTPS in front via Caddy/Traefik (currently HTTP-only).
- Auth on the dispatcher itself (anyone with network access can spawn a container).
- MinIO presigned-URL workflow + CORS (the prototypes use public URLs).
