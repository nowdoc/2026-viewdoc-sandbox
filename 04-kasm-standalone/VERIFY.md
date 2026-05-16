# Verification log

## HTTP-level (curl)

```bash
$ curl -i -H "Host: HOST:6081" "http://localhost:6081/?url=https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
HTTP/1.0 302 Found
Server: kasm-dispatcher/0.1 Python/3.12.13
Location: http://HOST:6082/?password=923c992405aa5fe6&autoconnect=1&resize=remote
X-Viewer-Container: 7e9a7bf56c70
X-Viewer-Name: kasm-viewer-883286af14
X-Viewer-Kind: doc
X-Viewer-Image: kasm2/chromium-notls:latest

$ curl -s -o /dev/null -w "%{http_code} %{content_type}\n" "http://localhost:6082/"
200 text/html       ← plain HTTP, no TLS, no basic-auth prompt
```

Verified:
- Location host matches the `Host` header (`HOST`) — LAN-friendly. ✅
- `?password=…&autoconnect=1&resize=remote` query is appended. ✅
- The viewer port serves plain HTTP. ✅

## Container spawn

```bash
$ docker ps --filter "label=dispatcher=kasm-standalone" --format "{{.Names}} {{.Image}} {{.Status}} {{.Ports}}"
kasm-viewer-883286af14 kasm2/chromium-notls:latest Up 31 seconds 0.0.0.0:6082->6901/tcp, [::]:6082->6901/tcp
```

Bound on `0.0.0.0` and `::` — accessible from any LAN interface.

## End-to-end visual (agent-browser)

Each of the four test URLs was opened with `agent-browser`. Screenshots in `screenshots/`.

| Type | Image | Screenshot | Result |
|---|---|---|---|
| PDF | `kasm2/chromium-notls` | `kasm-pdf.png` | Chromium PDF viewer renders "Dummy PDF file". URL bar shows w3.org. **No password prompt.** ✅ |
| PNG | `kasm2/chromium-notls` | `kasm-png.png` | Chromium image viewer renders the transparent-dice PNG. ✅ |
| MP3 | `kasm2/vlc-notls` | `kasm-mp3.png` | VLC playing SoundHelix-Song-1.mp3 — timeline shows 00:49/06:12. ✅ |
| MP4 | `kasm2/vlc-notls` | `kasm-mp4-no-dialog.png` | VLC playing Big Buck Bunny — green forest + bunny visible. Small "Errors" popup about audio decoder (KASM_SVC_AUDIO=disabled) is benign and dismisses on click. ✅ |

Notable:
- First MP4 screenshot (`kasm-mp4.png`) showed VLC's first-run "Privacy and Network Access Policy" modal — fixed by seeding `qt-privacy-ask=0` in `/home/kasm-default-profile/.config/vlc/vlcrc`.

## Iterations needed during build

1. `KASM_SVC_HTTPS=disabled` env did NOT disable HTTPS on `kasmweb/{chromium,vlc}:1.16.0` — required a wrapper image with `sed`+yaml patch.
2. HTTP `/` returns 200 but HTTP `HEAD /` returns 404 → diagnostic confusion. The dispatcher's readiness probe uses raw socket GET, so this didn't actually block.
3. First VLC wrapper wrote `vlcrc` into `/home/kasm-user/.config/` — broke the image's startup script which copies `/home/kasm-default-profile/` into `/home/kasm-user/`. Moved the file to the source path of that copy.

## Phase 2 additions — LibreOffice + Ubuntu desktop

After the initial 4-format build, two more wrapper images were added:

| Image | Triggered by | What it does |
|---|---|---|
| `kasm2/libreoffice-notls` | `?url=*.docx/xlsx/pptx/odt/ods/odp/rtf/csv` | Pre-downloads `$LAUNCH_URL` to `/tmp/payload.<ext>`, then opens in LibreOffice. Suppresses "Tip of the day"/first-run dialogs via seeded `registrymodifications.xcu`. |
| `kasm2/ubuntu-notls` | `/?desktop=ubuntu` | Full `kasmweb/ubuntu-jammy-desktop:1.16.0` — XFCE desktop with Firefox, Thunderbird, GIMP, VS Code, Sublime, Telegram, Chromium pre-installed. |

### DOCX validation

```bash
$ curl -i -H "Host: HOST:6081" "http://localhost:6081/?url=https://calibre-ebook.com/downloads/demos/demo.docx"
HTTP/1.0 302 Found
Location: http://HOST:6082/?password=...&autoconnect=1&resize=remote
X-Viewer-Image: kasm2/libreoffice-notls:latest
X-Viewer-Kind: office
```

Screenshot `screenshots/kasm-docx.png`: LibreOffice Writer with title bar `payload.docx — LibreOffice Writer`, "Demonstration of DOCX support in calibre" rendered, 9 pages / 1,642 words / 9,104 characters reported in the status bar. **No password prompt, no first-run welcome dialog.**

### Ubuntu desktop validation

```bash
$ curl -i -H "Host: HOST:6081" "http://localhost:6081/?desktop=ubuntu"
HTTP/1.0 302 Found
Location: http://HOST:6083/?password=...&autoconnect=1&resize=remote
X-Viewer-Image: kasm2/ubuntu-notls:latest
X-Viewer-Kind: desktop
```

Screenshot `screenshots/kasm-ubuntu.png`: Ubuntu Jammy Jellyfish wallpaper, Applications launcher top-left, desktop icons visible (Uploads, GIMP, Nextcloud sync, Thunderbird, Firefox, Telegram, VS Code, Sublime, Chromium, Remmina, Downloads). **No password prompt.**

### Dispatcher change

Added `/?desktop=<name>` path alongside `/?url=<asset>`:
- `_find_desktop_rule()` looks up rules whose `kind == "desktop"` and whose `extensions` list contains the desktop name (re-using the field as a name lookup table).
- The spawn flow is otherwise identical — same port allocation, password mint, redirect builder.

## What a human still needs to confirm

- Each viewer in a real browser at `http://HOST:6081/?url=<asset>`. The agent-browser screenshots confirm the auto-login + viewer rendering, but a real interaction (scrolling the PDF, scrubbing the video, dismissing the VLC error popup) is worth doing once before this is recommended for production.
