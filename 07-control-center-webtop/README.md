# 07 — control-center over LSIO webtop (MVP)

The 05 iframe-bridge architecture, rebased onto the LSIO webtop image so the
inner desktop has working audio/mic/uploads (which `kasmweb/*` lacks
standalone). Trimmed to the smallest thing that works:

- **One** fixed webtop container (no pool, no slot picker).
- **No LibreOffice** — Firefox + VLC + `xdg-open` only.
- **Two self-signed certs.** The webtop uses its built-in cert on 3001.
  The control-center generates its own on first boot (stored in
  `./certs/`). The control-center has to be HTTPS too — an HTTP parent
  page makes the embedded Selkies iframe a non-secure context (top-level
  cascade), and Selkies refuses to start. So the bootstrap is two
  cert-accepts the first time.

```
 user's browser
   │
   │  https://<host>:5087/?open_url=https://example.com/x.pdf
   ▼
 control-center (:5087, https, self-signed)
   │  POST /api/params
   │  ──► docker exec webtop-bridge-webtop /usr/local/bin/webtop-write-query
   ▼
 /tmp/webtop_query.{json,env}            (atomic)
   │
   ▼
 webtop-on-query-update.sh               (hook fires xdg-open / vlc)
   │
   ▼
 Firefox / VLC inside the iframe (https://<host>:5088/, LSIO webtop, Selkies)
```

## Run

```
cd 07-control-center-webtop
docker compose up -d --build
```

**One-time cert bootstrap** (per browser, both ports):

1. Open `https://<host>:5088/` → Advanced → Proceed. Trusts the webtop cert.
2. Open `https://<host>:5087/` → Advanced → Proceed. Trusts the
   control-center cert.

After that:

```
https://<host>:5087/?open_url=https://example.com
```

The iframe loads the webtop, the URL watcher POSTs `{open_url: …}`, and
Firefox inside the inner desktop opens the URL.

## What you can do

- `?open_url=https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf`
  → opens the PDF in Firefox inside the desktop.
- `?open_url=https://example.com/foo.mp4` → opens in VLC (extension routing).
- Any other key in the query string is just stored in
  `/tmp/webtop_query.{json,env}` for in-desktop apps to read.

## Endpoints

| Method | Path           | Description                                       |
| ------ | -------------- | ------------------------------------------------- |
| GET    | `/`            | Static page (iframe + URL watcher)                |
| GET    | `/static/*`    | Static assets                                     |
| GET    | `/healthz`     | `{ok, container}`                                 |
| POST   | `/api/params`  | Body `{params: {...}}` — writes inside the webtop |

## In-container contract

| File                                          | Purpose                                                                  |
| --------------------------------------------- | ------------------------------------------------------------------------ |
| `/usr/local/bin/webtop-write-query`           | Atomic JSON-blob → `/tmp/webtop_query.{json,env}` writer.                |
| `/usr/local/bin/webtop-on-query-update.sh`    | Hook. Default: opens `WEBTOP_Q_OPEN_URL` in vlc (media) or xdg-open.     |
| `/tmp/webtop_query.json`                      | `{params, raw}` — pretty-printed.                                        |
| `/tmp/webtop_query.env`                       | `WEBTOP_Q_<UPPER>=<value>` — single-quoted, source-able.                 |
| `/tmp/on_query_update.log`                    | Hook stdout/stderr (debug).                                              |

## Limits (MVP)

- One fixed container. Concurrent tabs share it — last write wins.
- No auth on :5087.
- No idle/busy tracking, no per-tab affinity.
- Selkies inside the iframe still requires a secure context. We solve that
  with the cert-bootstrap step; a real deployment should front the whole
  stack with a TLS-terminating reverse proxy.
- No LibreOffice opener (skipped to keep the apt-install thin). Add it back
  by extending the case in `images/on_query_update.sh` + adding
  `libreoffice` to the apt-install line in `images/Dockerfile.webtop`.
- No automatic state reset. Each `docker compose down` + `rm -rf ./config`
  resets the webtop's user profile.

## File map

```
07-control-center-webtop/
├── README.md
├── docker-compose.yml
├── images/
│   ├── Dockerfile.webtop          # FROM lscr.io/.../webtop:ubuntu-xfce + vlc + xdg-utils + helper + hook
│   ├── webtop-write-query         # /tmp/webtop_query.{json,env} writer
│   └── on_query_update.sh         # opens WEBTOP_Q_OPEN_URL via vlc / xdg-open
└── server/
    ├── Dockerfile
    ├── server.py                  # ~110 lines, stdlib
    └── static/
        ├── index.html
        └── app.js
```
