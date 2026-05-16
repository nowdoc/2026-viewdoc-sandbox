# xpra-viewer prototype

One-asset-per-container viewer built on top of [xpra](https://xpra.org)'s HTML5
client. A dispatcher receives `GET /?url=<asset>`, spawns a disposable
Debian-based container that opens the asset under xpra (`xpdf` for PDFs,
`feh` for images, `mpv` for audio/video), then 302-redirects the browser to
the host port serving the xpra HTML5 client. No password
(`--tcp-auth=none --ws-auth=none`).

The entrypoint **downloads** for local-file viewers (xpdf/feh) and **streams
the URL directly** for `mpv`, so a 250MB MP4 doesn't block xpra startup.

## Components

| Component   | Path                        | Purpose                                         |
|-------------|-----------------------------|-------------------------------------------------|
| viewer img  | `images/Dockerfile.viewer`  | Debian + xpra + xpra-html5 + xpdf/feh/mpv       |
| entrypoint  | `images/entrypoint.sh`      | Downloads asset, picks viewer, launches xpra    |
| dispatcher  | `dispatcher/dispatcher.py`  | HTTP service, `docker run`s viewers, 302s back  |
| compose     | `docker-compose.yml`        | Brings up the dispatcher only                   |

The dispatcher is what `docker compose up` starts. The viewer image is built
once by `./build.sh` and then `docker run` is invoked by the dispatcher per
request.

## Quick start

```bash
./build.sh                 # builds xpra-viewer + xpra-dispatcher
docker compose up -d       # starts the dispatcher on :9081

# from any machine on the LAN:
curl -i "http://<host-ip>:9081/?url=https://example.com/sample.pdf"
# 302 Location: http://<host-ip>:<ephemeral>/
# then open that URL in a browser
```

## Network accessibility

This prototype is designed to be reached from other machines on the LAN, not
just `localhost`. The deliberate choices are:

1. **Dispatcher port mapping is unbound by IP.** `docker-compose.yml` uses
   `9081:9081` (no `127.0.0.1:` prefix), so docker binds on `0.0.0.0` and
   `::`. Verify with `docker port xpra-dispatcher`.
2. **Viewer containers publish on all interfaces.** The dispatcher spawns each
   viewer with `-p <picked_port>:14500` (no bind-IP), where `<picked_port>` is
   selected from the reserved range (default `9082-9099`, see
   `VIEWER_PORT_RANGE_START`/`END`). Verify with
   `docker inspect <viewer> | jq '.[0].NetworkSettings.Ports'`; you will see
   bindings on `0.0.0.0` and `::`, not `127.0.0.1`.
3. **The 302 Location uses the request's `Host` header.** A client coming
   from `http://<your-host>:9081/?...` is redirected to
   `http://<your-host>:<picked_port>/`, not `http://localhost:...`. Override
   with the `PUBLIC_HOST` env var if Host is unreliable in your deployment
   (e.g. behind certain reverse proxies).

### Firewall

The host firewall must allow inbound TCP on:

- **`9081/tcp`** — the dispatcher.
- **`9082-9099/tcp`** — the host-port range from which the dispatcher hands
  out ports to viewer containers. The range is configurable in
  `docker-compose.yml` via `VIEWER_PORT_RANGE_START` and `VIEWER_PORT_RANGE_END`;
  keep your firewall rule in sync if you change it.

For UFW:

```bash
sudo ufw allow 9081/tcp
sudo ufw allow 9082:9099/tcp
```

For firewalld:

```bash
sudo firewall-cmd --permanent --add-port=9081/tcp
sudo firewall-cmd --permanent --add-port=9082-9099/tcp
sudo firewall-cmd --reload
```

The 9082-9099 window caps concurrent viewers at 18; widen
`VIEWER_PORT_RANGE_END` (and your firewall) if you need more.

## Environment

`docker-compose.yml` knobs on the dispatcher:

| Var                  | Default          | Meaning                                                                                  |
|----------------------|------------------|------------------------------------------------------------------------------------------|
| `DISPATCHER_PORT`        | `9081`               | TCP port the dispatcher listens on.                                                          |
| `VIEWER_IMAGE`           | `xpra-viewer:latest` | Image used for `docker run`.                                                                 |
| `XPRA_CONTAINER_PORT`    | `14500`              | Container-internal xpra port (matches Dockerfile's `EXPOSE`).                                |
| `IDLE_TTL`               | `900`                | Seconds before the janitor kills a viewer container.                                         |
| `PUBLIC_HOST`            | *(empty)*            | If set, used as the host in 302 Location instead of the request's Host header.               |
| `VIEWER_PORT_RANGE_START`| `9082`               | First host port the dispatcher will assign to a viewer.                                      |
| `VIEWER_PORT_RANGE_END`  | `9099`               | Last host port (inclusive). Keep the firewall rule and this value in sync.                   |

## Verification

```bash
# Dispatcher is reachable on every interface, not just localhost:
docker port xpra-dispatcher
# 9081/tcp -> 0.0.0.0:9081
# 9081/tcp -> [::]:9081

# Hit the dispatcher with the host's LAN IP and check the Location header:
HOST_IP=$(ip -4 -o route get 1.1.1.1 | awk '{print $7}')
curl -sD - -o /dev/null "http://$HOST_IP:9081/?url=https://example.com/sample.pdf" | grep -i '^location:'
# location: http://<HOST_IP>:<picked_port>/    <-- NOT localhost, picked_port in 9082-9099

# Inspect the spawned viewer:
CID=$(docker ps --filter "ancestor=xpra-viewer:latest" -q | head -1)
docker inspect "$CID" --format '{{json .NetworkSettings.Ports}}' | jq
# {"14500/tcp":[{"HostIp":"0.0.0.0","HostPort":"9082"},
#               {"HostIp":"::","HostPort":"9082"}]}
```

## Caveats / known limits

- No auth on viewer containers (`--tcp-auth=none`). Access control in this
  prototype is "knowledge of the ephemeral port number"; OK for LAN demo,
  **not** OK for the open internet. Put it behind a reverse proxy with auth
  for anything real.
- The janitor reaps by age, not by activity. A user leaving a tab open will
  still get GC'd after `IDLE_TTL`.
- One viewer per asset URL: hitting `/?url=X` twice spawns two containers.
- xpra-html5 is served over plain HTTP; the prototype does not terminate TLS.
