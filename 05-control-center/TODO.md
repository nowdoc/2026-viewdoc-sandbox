# 05 — control-center (iframe bridge)

## Goal

A thin web UI at `http://HOST:5081/` that **wraps the kasm viewer in an iframe** and acts as a bridge: it reads its own browser URL (the user's address bar) and forwards changes into the running kasm container in real time.

Builds on `04-kasm-standalone/`. Does **not** patch NoVNC.

Real-world test: user opens `http://HOST:5081/?desktop=ubuntu&theme=dark&user=foo`. Inside the kasm container, `/tmp/kasm_query.json` reflects `{theme: dark, user: foo}` at spawn time AND updates if the user later changes the address bar to add `&note=hello`.

## Architecture (recommended)

```
 User's Chrome  ──── http://HOST:5081/?desktop=ubuntu&foo=bar ────►
                           ↓
                    control-center page (static HTML+JS, served by a tiny FastAPI/stdlib server)
                           ↓ 1) POST /api/session  {desktop, url, params}
                           ↓ 2) gets back {session_id, viewer_url, vnc_pw}
                           ↓ 3) renders <iframe src="<viewer_url>">
                           ↓ 4) watches its own URL via popstate / hashchange / poll
                           ↓ 5) POST /api/session/<id>/params  {params}
                           ↓
                    dispatcher (or new control-center server)
                           ↓ docker exec into the kasm container
                           ↓ writes /tmp/kasm_query.json + /tmp/kasm_query.env
                           ↓ optionally fires a hook script inside the container
                           ↓
                    kasm container (ubuntu-notls / chromium-notls / etc.)
                           ↑ apps inside read /tmp/kasm_query.json
```

The win: **no NoVNC patching, no postMessage gymnastics, no new port per container.** The control-center page does the watching, the existing dispatcher does the injection.

## Folder layout (target)

```
/srv/kasm2/05-control-center/
├── TODO.md                    # this file
├── PLAN.md                    # architectural decisions (write after first build)
├── README.md                  # how to run + test
├── VERIFY.md                  # verification log + agent-browser screenshots
├── docker-compose.yml         # control-center server + (optional) dispatcher reuse
├── server/
│   ├── Dockerfile
│   ├── server.py              # FastAPI or stdlib HTTP, ~200 lines
│   └── static/
│       ├── index.html         # the control-center page
│       ├── app.js             # iframe management + URL watcher + fetch /api/sync
│       └── style.css
└── screenshots/               # agent-browser validations
```

## Tasks (in order)

### 1. Control-center HTTP server (port 5081, 0.0.0.0)

- Stdlib HTTP or FastAPI. Whatever's smallest.
- Endpoints:
  - `GET /` → serve `static/index.html` (and bundle URL params in the page, so JS sees them on first load).
  - `GET /static/*` → static assets.
  - `POST /api/session` → body `{desktop, url, params}`. Spawns viewer via the existing `04-kasm-standalone` dispatcher (HTTP call internally, or call docker directly — pick one). Returns `{session_id, container_id, viewer_url, vnc_pw}`. `session_id` should equal the container id for simplicity.
  - `POST /api/session/<container_id>/params` → body `{key: value, ...}`. Sanitises keys/values like `04-kasm-standalone/dispatcher/dispatcher.py:build_passthrough_env`, then `docker exec <id>` to update `/tmp/kasm_query.json` + `/tmp/kasm_query.env` atomically.
  - `GET /healthz`.
- **CRITICAL**: bind on `0.0.0.0`. Same Host-header redirect logic as the other prototypes. Honour `PUBLIC_HOST` env.
- Use the existing `04` dispatcher's `build_passthrough_env` helper — don't re-implement. Import or shell-call.

### 2. Static page (`index.html` + `app.js`)

- On load:
  - Read `window.location.search`.
  - Pull out `desktop` / `url` (reserved, sent to spawn API as-is).
  - Everything else goes into `params`.
  - `fetch('/api/session', {method:'POST', body: JSON.stringify({desktop, url, params})})`.
  - Save returned `container_id` to a module-scoped variable.
  - Set `<iframe src="<viewer_url>" style="…">` filling the viewport.
- Continuously watch the user's URL:
  - Listen for `popstate` and `hashchange`.
  - Also short-interval poll `window.location.href` (every 1 s) as a fallback — popstate doesn't fire on pure search-string mutation by other JS.
  - On change, diff against the last-known params, fetch `/api/session/<id>/params` with the new dict.
- Optional bookmarklet in the UI: a button that copies a JS snippet users can paste in their console to mutate the URL (`history.replaceState(...)`) — useful for demoing the round-trip without a real source of URL changes.
- Tiny status bar overlay (top-right, semi-transparent, ~24px tall): shows `session_id` + last sync timestamp + last error.

### 3. Docker exec injector (server side)

- Implement `POST /api/session/<id>/params`:
  - Validate `id` matches a label-filtered container (`dispatcher.session=<id>` or `dispatcher=kasm-standalone`).
  - Build the same env-mapping as `build_passthrough_env`.
  - `docker exec <id> /usr/local/bin/kasm-write-query <json-blob>`.
- Build the `/usr/local/bin/kasm-write-query` script into the kasm wrapper images (`Dockerfile.ubuntu`, optionally `.chromium`/`.vlc`/`.libreoffice` too). It does:
  - Atomic write: temp file → `mv` to `/tmp/kasm_query.json` + `/tmp/kasm_query.env`.
  - Optional: signal a hook (`/dockerstartup/on_query_update.sh` if present) so apps can react.

### 4. Hook script (optional, in `ubuntu-notls`)

- `/dockerstartup/on_query_update.sh` — runs after every successful `kasm-write-query`. Default behaviour: do nothing. Documented as the extension point.
- Demo hook: if `KASM_Q_OPEN_URL` changes, call the existing type-aware opener (firefox/libreoffice/vlc) on the new value. This makes `?desktop=ubuntu&open_url=X` change the open document on the fly.

### 5. End-to-end test (use `agent-browser`)

Test plan:

1. Open `http://HOST:5081/?desktop=ubuntu&theme=dark`.
2. Wait for iframe + ubuntu desktop to boot. Screenshot.
3. `docker exec <container> cat /tmp/kasm_query.json` → expect `theme: dark` immediately.
4. From within agent-browser, evaluate JS:
   ```js
   const u = new URL(window.location); u.searchParams.set('user', 'milan'); history.replaceState({}, '', u.toString());
   ```
5. Wait ~2 s for the URL-watcher to fire.
6. `docker exec <container> cat /tmp/kasm_query.json` → expect `theme: dark, user: milan`.
7. Screenshot before/after.

If we wire the `on_query_update.sh` hook, also test:

8. JS: set `open_url=https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf` in the URL.
9. Confirm Firefox inside the desktop opens the PDF.

### 6. Documentation

- `PLAN.md` — what got built, what was deferred, the postMessage alternative if relevant.
- `README.md` — start commands, test URLs.
- `VERIFY.md` — capture the 8-step test outcomes with screenshots in `screenshots/`.

## Tech choices / constraints

- **Bind on `0.0.0.0`** for everything published. Honour `PUBLIC_HOST` env. Mirror the Host-header redirect pattern from `02-xpra/dispatcher/dispatcher.py` and `04-kasm-standalone/dispatcher/dispatcher.py`.
- **Port allocation**: 5081 = control-center HTTP. 5082-5099 reserved for any sidecar (currently none planned). Avoid 3000, 6081, 6082-6099, 7080-81, 8080-81, 9081, 9082-9099, 32768-32770.
- **Docker socket**: yes, mount `/var/run/docker.sock` so the control-center can `docker exec` into spawned containers. Same risk as the existing dispatcher.
- **Talking to 04**: simplest is to keep 04 running on 6081 and have 05 call its `/?desktop=...&url=...` endpoint via HTTP, then parse the `Location:` header. Then 05 owns the live-update path. Alternative: 05 reimplements the spawn logic. Pick whichever is less code — recommend HTTP call to 04.
- **Auth**: none for v1. Anyone on the LAN can spawn / mutate. Document as v1 limitation.
- **iframe security**: set `sandbox="allow-same-origin allow-scripts allow-forms allow-popups"` on the iframe so the kasm NoVNC client still works but it can't navigate the parent.
- **CORS**: control-center serves the page and the API on the same origin (5081), so no CORS dance needed. If the iframe ever needs to talk back to the parent, use `postMessage` (not strictly required for v1).

## Open questions

1. **Update granularity** — push every URL change, or debounce? Recommend ~500 ms debounce so rapid keystrokes don't spam `docker exec`.
2. **Which params trigger an in-session action vs. just sit in `/tmp/kasm_query.json`?** Default: passive (just file write). Make the `on_query_update.sh` hook the documented extension point.
3. **Should the iframe URL include the original query params?** Currently `04-kasm-standalone` strips them. 05 doesn't need them in the iframe URL (it has them as JS state in the parent), so no change to 04 needed.
4. **postMessage as a future enhancement** — would let the iframe TALK BACK to the control-center (e.g., kasm side reports something). Not in scope for v1; document as future.
5. **What if the kasm container restarts mid-session?** Detection + reconnect logic out of scope for v1. Show an error in the status bar.

## What `agent-browser` can validate

- ✅ Iframe loads, kasm NoVNC client connects.
- ✅ `/tmp/kasm_query.json` is updated on spawn (verifiable via `docker exec`).
- ✅ `/tmp/kasm_query.json` is updated when JS mutates the parent URL (verifiable via `docker exec`).
- ✅ With the `on_query_update.sh` hook wired, an `?open_url=` change reopens the file in Firefox inside the desktop.

## Existing assets to lean on (in `/srv/kasm2/`)

- `04-kasm-standalone/dispatcher/dispatcher.py` — `build_passthrough_env()` helper.
- `04-kasm-standalone/images/ubuntu-custom-startup.sh` — already writes `/tmp/kasm_query.{env,json}` at spawn time. Reuse the file format.
- `04-kasm-standalone/dispatcher/mapping.yaml` — for the desktop-name lookup if 05 reimplements spawn.
- `01-neko-rooms/dispatcher/dispatcher.py:rewrite_location` — Host-header redirect template.
- `agent-browser` CLI (`/usr/local/bin/agent-browser`) — for E2E validation. Use `set viewport 1280 800`, `open <url>`, `eval <js>`, `screenshot <path>`.

## Out of scope

- Auth on 5081.
- Multi-user / session sharing.
- Bidirectional postMessage (kasm → control center).
- Reconnect on container restart.
- Patching NoVNC.
- Reverse proxy / TLS (use a Caddy in front later).
