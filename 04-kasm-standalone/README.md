# 04-kasm-standalone

URL-to-viewer dispatcher built on `kasmweb/*` Docker images. No KASM Workspaces server, no Postgres, no orchestrator.

```
GET http://HOST:6081/?url=<asset-url>
→ 302 http://HOST:<6082-6099>/?password=<pw>&autoconnect=1&resize=remote
```

`HOST` is whichever hostname or IP the docker host is reachable on for
your clients — `localhost` if you're hitting it from the same machine,
the LAN IP if you're hitting it from another box, or a DNS name if you
front it with one. The dispatcher echoes the request's `Host` header
back into the 302, so it just works without configuration.

## Quick start

```bash
docker compose up -d --build

# build the no-TLS wrapper images (once; ~30 s each first time)
docker build -t kasm2/chromium-notls:latest -f images/Dockerfile.chromium images/
docker build -t kasm2/vlc-notls:latest     -f images/Dockerfile.vlc      images/
```

## Test URLs

| Type | URL |
|---|---|
| PDF | `http://HOST:6081/?url=https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf` |
| PNG | `http://HOST:6081/?url=https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png` |
| MP3 | `http://HOST:6081/?url=https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3` |
| MP4 | `http://HOST:6081/?url=https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4` |
| DOCX | `http://HOST:6081/?url=https://calibre-ebook.com/downloads/demos/demo.docx` |
| XLSX | `http://HOST:6081/?url=https://file-examples.com/wp-content/storage/2017/02/file_example_XLSX_10.xlsx` |
| Ubuntu desktop | `http://HOST:6081/?desktop=ubuntu` |
| Ubuntu + PDF | `http://HOST:6081/?desktop=ubuntu&url=https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf` |
| Ubuntu + DOCX | `http://HOST:6081/?desktop=ubuntu&url=https://calibre-ebook.com/downloads/demos/demo.docx` |
| Ubuntu + MP4 | `http://HOST:6081/?desktop=ubuntu&url=https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4` |

## Forwarding arbitrary query params into the kasm session

Any query param besides `desktop`, `url`, `u` is forwarded to the spawned container as env vars and on-disk files. So:

```
http://HOST:6081/?desktop=ubuntu&theme=dark&user=milan&token=abc123
```

…produces inside the container:

```
$ env | grep KASM_Q
KASM_Q_THEME=dark
KASM_Q_USER=milan
KASM_Q_TOKEN=abc123
KASM_QUERY=theme=dark&user=milan&token=abc123     # URL-encoded form
KASM_QUERY_KEYS=theme user token

$ cat /tmp/kasm_query.env       # shell-sourceable
$ cat /tmp/kasm_query.json      # {"params": {...}, "raw": "..."}
```

Keys must match `[A-Za-z][A-Za-z0-9_]{0,63}`; values are truncated to 4096 bytes; max 32 keys per request. Apps inside the desktop can read these to drive theme/auth/locale/etc.

Routing:
- PDF + images → Chromium (built-in PDF viewer; `LAUNCH_URL` env)
- Audio + video → VLC (`APP_ARGS` env)
- `docx/xlsx/pptx/odt/ods/odp/rtf/csv` → LibreOffice (URL pre-downloaded to `/tmp/payload.<ext>` by the wrapper image, then passed as a file path)
- `?desktop=ubuntu` → full `kasmweb/ubuntu-jammy-desktop:1.16.0` session (Firefox, Thunderbird, GIMP, VS Code, Sublime, Telegram, Chromium, etc., pre-installed)
- Default fallback → Chromium

## Configuration (`.env`)

| Var | Default | Notes |
|---|---|---|
| `DISPATCHER_PORT` | `6081` | Dispatcher listen port (0.0.0.0). |
| `VIEWER_PORT_RANGE_START` | `6082` | First port handed to a viewer container. |
| `VIEWER_PORT_RANGE_END` | `6099` | Last port. Same range = max concurrent sessions. |
| `PUBLIC_HOST` | _(empty)_ | Override the redirect host. Empty = derive from request `Host` header (LAN-friendly). |
| `KASM_SVC_HTTPS_VALUE` | `disabled` | Passed through as env to spawned containers (cosmetic — the wrapper images already disable TLS via `sed`+yaml). |
| `READY_TIMEOUT_S` | `60` | Seconds to wait for the spawned container's NoVNC port to respond. |
| `VIEWER_MEMORY` / `VIEWER_CPUS` | `2g` / `2.0` | Per-container caps. Chromium needs ~1 GB warm. |

## Firewall (LAN access)

Allow inbound:
- `6081/tcp` — dispatcher
- `6082-6099/tcp` — viewer containers

The dispatcher binds all interfaces; the redirect URL uses the request's Host header, so a LAN client at `192.168.x.y:6081` stays on that host for the redirect.

## Manual cleanup

```bash
docker ps --filter "label=dispatcher=kasm-standalone" -q | xargs -r docker rm -f
```

Spawned viewers are `docker run --rm`, so they vanish on stop.

## Files

```
04-kasm-standalone/
├── PLAN.md                       # architecture + decisions
├── README.md                     # this file
├── VERIFY.md                     # curl + agent-browser verification log
├── docker-compose.yml            # dispatcher only
├── .env                          # config overrides
├── dispatcher/
│   ├── Dockerfile                # python:3.12-slim + static docker CLI
│   ├── dispatcher.py             # 420-line stdlib HTTP service
│   └── mapping.yaml              # ext → image rules (data-driven)
├── images/
│   ├── Dockerfile.chromium       # patches kasmweb/chromium:1.16.0 to disable TLS
│   ├── Dockerfile.vlc            # patches kasmweb/vlc:1.16.0 + seeds vlcrc
│   ├── Dockerfile.libreoffice    # wraps kasmweb/libre-office:1.16.0 (URL pre-download)
│   ├── libreoffice-custom-startup.sh  # replaces /dockerstartup/custom_startup.sh
│   ├── Dockerfile.ubuntu         # wraps kasmweb/ubuntu-jammy-desktop:1.16.0
│   └── kasmvnc-no-tls.yaml       # KasmVNC config override (no require_ssl)
└── screenshots/                  # agent-browser visual verification
```
