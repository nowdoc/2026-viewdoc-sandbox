# 06 — LinuxServer.io webtop (standalone audio test)

Smallest possible self-contained desktop-in-a-browser, used as a sanity
check for the "audio doesn't work standalone" limitation hit in `05`.

Background: Kasm-published images (`kasmweb/*`) carry audio over a
separate websocket that only the full Kasm Workspaces platform wires up.
A naked `docker run -p 6901:6901 kasmweb/...` gives you silent video.

LinuxServer.io's webtop image solves the same problem by **shipping the
proxying glue inside the container**: an NGINX layer + a helper that wires
the audio side-channel to the web UI. The historical implementation was
KasmVNC + Kclient; since June 2025 LSIO has rebased webtop on **Selkies
(WebRTC)**, which streams audio inline in the same channel as the video.

Either way, the user experience is the same: open one URL, get a desktop
with working audio, no orchestrator required.

## Run

```
cd 06-lsio-webtop
docker compose up -d
```

Web UI (substitute `HOST` with `localhost` / your LAN IP / your DNS name):

- http://HOST:5085/   (HTTP — fine on localhost / LAN)
- https://HOST:5086/  (HTTPS, self-signed cert)

To clean up:

```
docker compose down
sudo rm -rf ./config
```

## What to look for

- The webtop client renders an XFCE desktop in the browser.
- There's a sidebar/toolbar with **audio toggle** + **microphone toggle**
  controls. (This is the part `05` is missing.)
- Open Firefox in the desktop → play any YouTube video → audio comes out
  of the host browser.

## Ports

| Host  | Container | What                         |
| ----- | --------- | ---------------------------- |
| 5085  | 3000      | HTTP web UI (Selkies)        |
| 5086  | 3001      | HTTPS web UI (self-signed)   |

5085-5086 was picked to stay clear of 5081-5084 used by `05`.

## Why this isn't a drop-in replacement for the `05` pool

The `05-control-center` iframe-bridge expects:

1. A predictable per-slot HTTP port (one container → one port). Webtop
   matches that.
2. The ability to `docker exec` `/usr/local/bin/kasm-write-query` and the
   `on_query_update.sh` hook to receive URL-param updates. Webtop has
   neither — we'd port them into a derived `Dockerfile` on top of
   `lscr.io/linuxserver/webtop:ubuntu-xfce`.
3. The KasmVNC autoconnect query-string (`?password=…&autoconnect=1`).
   Webtop's Selkies client takes different params (it has its own
   sign-in/passwordless modes). The iframe `src` builder in
   `server.py` would need to learn that variant.

So adopting webtop as the `05` pool image is feasible but non-trivial —
roughly a fork of `images/Dockerfile.ubuntu` rebased on the LSIO image,
plus a small change in `server.py`'s `viewer_url` construction. This `06`
folder is intentionally **just the bare image**, to first confirm audio
works at all before committing to the integration work.
