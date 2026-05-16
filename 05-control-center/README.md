# 05 — control-center

Iframe-bridge UI on port **5081**. Owns a small **pool of pre-warmed
kasm/ubuntu containers**, leases one per browser session, and forwards the
user's browser-URL changes into the leased container in real time. Fully
self-contained — no dependency on 04.

```
 user's browser ── http://HOST:5081/?desktop=ubuntu&theme=dark
                       │
                       ▼
              control-center (this prototype, :5081)
                       │  POST /api/session  ─── lease next pool slot
                       │  ──► docker exec kasm-write-query  (init /tmp/kasm_query.*)
                       │  ──► return viewer_url for that slot
                       │
                       ▼
              <iframe src="http://…:5082/?password=…&autoconnect=1">
                       │
              [popstate / hashchange / 1 s href poll / URL-input edits]
                       │
                       │  POST /api/session/<id>/params   (~500ms debounce)
                       ▼
              control-center  docker exec kasm-write-query
                       │
                       ▼
              /tmp/kasm_query.json + /tmp/kasm_query.env  (atomic, shell-sourceable)
                       │
                       ▼
              /dockerstartup/on_query_update.sh  (optional hook; default
              demo: opens KASM_Q_OPEN_URL in firefox / libreoffice / vlc)

  Pool (default size 3):
    slot 0  →  kasm-cc-default-0  on host port 5082
    slot 1  →  kasm-cc-default-1  on host port 5083
    slot 2  →  kasm-cc-default-2  on host port 5084
```

## Run

```
cd 05-control-center
cp .env.example .env                   # (optional)

# 1) Build the pool image (one-off).
docker compose --profile build up --build image-builder
# Equivalent: docker build -t kasm2-cc/ubuntu:latest images/

# 2) Bring up the control-center. The server spawns the pool at startup
#    and waits for each slot to serve HTTP before listening on 5081.
docker compose up -d --build

# 3) Hit it.
open http://HOST:5081/?desktop=ubuntu&theme=dark&user=milan
```

## What you can do from the page

- Edit the URL in the navbar input → press Enter to push it to the address
  bar; the watcher hot-patches the kasm container.
- Edit the URL directly in the browser address bar (or via
  `history.replaceState`) → same effect; the 1 s href poll + 500 ms debounce
  catches it.
- "copy demo snippet" button in the navbar puts a ready-to-paste JS line on
  your clipboard for the dev-console demo.

## Endpoints

| Method | Path                                 | Description |
| ------ | ------------------------------------ | ----------- |
| GET    | `/`                                  | Static page (`index.html` + `app.js`) |
| GET    | `/static/*`                          | Static assets |
| GET    | `/healthz`                           | JSON: pool readiness + slot list |
| POST   | `/api/session`                       | Lease a pool slot. Body: `{desktop?, url?, params}` |
| POST   | `/api/session/<container_id>/params` | Hot-patch `/tmp/kasm_query.{json,env}`. Body: `{params}` |

`url` in the spawn body is treated as a shortcut for `params.open_url` — the
desktop is already up, so opening anything happens through the in-container
`on_query_update.sh` hook (which already reacts to `KASM_Q_OPEN_URL`).

## In-container contract

The pool image (`kasm2-cc/ubuntu:latest`, built from `images/Dockerfile.ubuntu`)
ships:

| File / script                          | Purpose |
| -------------------------------------- | ------- |
| `/usr/local/bin/kasm-write-query`       | Atomic JSON-blob → `/tmp/kasm_query.{json,env}` writer. Single-quotes env values so the file is safely `source`-able. Fires the update hook. |
| `/dockerstartup/on_query_update.sh`     | Extension hook. Default behaviour: reopen `KASM_Q_OPEN_URL` in firefox / libreoffice / vlc when it changes. Override in derived images. |
| `/dockerstartup/custom_startup.sh`      | Boot-time: writes the initial `/tmp/kasm_query.*` from env and (optional) opens `LAUNCH_URL`. |
| `/etc/kasmvnc/kasmvnc.yaml` + entrypoint patch | Disables TLS so the iframe loads via plain http. |

## Yes — open real software in the desktop

`?desktop=ubuntu&open_url=https://example.com/x.pdf` makes the in-container
hook open the URL in firefox (PDFs/HTML/images) or libreoffice (office docs)
or vlc (media). Change the URL → the hook re-opens. This is wired and tested
(see `VERIFY.md`).

## Clipboard

Clipboard is **on by default** in both directions — KasmVNC handles it
natively, the control-center only needs to delegate the Permissions Policy
to the iframe (`allow="clipboard-read; clipboard-write"`).

For seamless `ctrl-c` / `ctrl-v` across the iframe boundary the **parent
page must be in a secure context** (HTTPS or `localhost`). The iframe
itself is now always **same-origin** as the parent — the control-center
reverse-proxies `/slot/<N>/*` to the slot's KasmVNC (HTTP + WebSocket
Upgrade), so the iframe inherits the parent's origin and cert. No extra
cert prompts, no per-port acceptance dance.

| You hit                                          | Clipboard? |
| ------------------------------------------------ | ----------- |
| `https://<host>:5081/…`                          | yes — accept the control-center's self-signed cert once, done |
| `http://localhost:5081/…`                        | yes (`localhost` is treated as secure even over plain http) |
| `http://<lan-host>:5081/…`                       | sidebar widget only — Clipboard API is blocked outside a secure context |

If you sit a real-cert reverse proxy (Caddy / Tailscale Funnel /
cloudflared) in front of the control-center, clipboard works with no
prompts at all — the proxy adds the trusted cert at the edge and the
same-origin model carries it through to the iframe.

KasmVNC's sidebar clipboard widget still works in any context — open the
side panel → "Clipboard" tab — but it's a manual textarea, not seamless.

Per-direction byte cap is 10 MiB
(`images/kasmvnc-no-tls.yaml` → `data_loss_prevention.clipboard.*`).
Override via env or edit the yaml.

### TLS cert

A self-signed cert is generated at first boot and persisted in the
`cc-state` docker volume (`/app/state/control-center.{crt,key}`). The
default SAN covers `localhost` and the loopback IPs. Add your own
hostnames (the LAN name you'll be hitting it on, your `.lan` mDNS name,
etc.) by setting `TLS_CERT_SAN` in `.env` to an OpenSSL `subjectAltName`
string before first start, e.g.:

```
TLS_CERT_SAN=DNS:localhost,DNS:viewer.lan,DNS:*.viewer.lan,IP:127.0.0.1,IP:::1
```

To force regeneration after changing it: `docker volume rm
05-control-center_cc-state` and restart.

## Stream quality

The pool image ships with high-quality defaults baked in
(`MAX_FRAME_RATE=30`, `VNCOPTIONS=-DynamicQualityMin=7 -DynamicQualityMax=9
-DLP_ClipDelay=0 -DLP_ClipSendMax=…`). The viewer URL the control-center
hands the browser also carries matching NoVNC URL params
(`quality=9&compression=2&dynamic_quality_min=7&dynamic_quality_max=9&
prefer_local_cursor=1&clipboard_seamless=1`).

To trade quality for bandwidth (slow links, lots of pool slots, etc.),
override the env knobs in `.env` — see `.env.example` for the full list.
The `kasmvnc.yaml`'s `runtime_configuration.allow_override_list` is widened
so URL-param overrides reach the server.

## Limits (v1)

- No auth on 5081 — anyone on the LAN can lease and mutate slots.
- No idle/busy slot tracking. Round-robin lease: 4 tabs on a pool of 3
  means tab 4 shares a slot with tab 1, last write wins.
- No per-tab affinity across page reloads; you may land on a different slot.
- No automatic state reset between leases. A previous user's open
  windows are still there. To reset, `docker compose restart` (re-spawns
  the pool fresh).
- No reconnect logic if a kasm slot crashes; healthz will still list it.
