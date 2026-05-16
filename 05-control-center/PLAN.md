# 05 — control-center · architectural notes

## What we picked

A **self-contained iframe-bridge** with a small **pre-warmed container pool**:

1. The control-center server (Python stdlib, no FastAPI) owns N kasm/ubuntu
   containers from startup, on deterministic host ports.
2. A static page in the same origin watches the user's address bar.
3. URL changes → debounced POST to the server → `docker exec` writes
   `/tmp/kasm_query.{json,env}` inside the leased container atomically →
   optional in-container hook reacts.

The TODO laid out three options (NoVNC patch, postMessage, parent-URL +
docker-exec). We took the third + collapsed to a pool architecture after a
follow-up decision.

## Why a pool, not dynamic spawn

- **Latency.** Kasm cold-start is 10-25s. With a pool, `/api/session`
  returns in milliseconds — no boot wait inside the user flow.
- **Predictability.** Fixed slot count, fixed port range, predictable
  resource ceiling.
- **Simpler error model.** No port-pick failures, no per-request docker
  run lifetime to chase.

Trade-offs we accepted:
- **State leaks between leases.** Round-robin reuses slots; the next user
  inherits whatever the previous user did. v1 mitigation: `docker compose
  restart` re-spawns the pool fresh. A real product would reset
  per-lease or use idle/busy tracking with a release signal.
- **Fixed ceiling.** If N tabs > POOL_SIZE, tabs share slots. Last write
  wins.

## Why this is independent of 04

Earlier the server forwarded `/api/session` to the 04 dispatcher over HTTP.
That coupled deployments: the 04 stack had to be up, on a shared
`kasm-net` docker network, and 04 owned the ubuntu image. Now:

- 05 builds its own image (`kasm2-cc/ubuntu:latest`) from `images/`. The
  ubuntu wrapper assets (`ubuntu-custom-startup.sh`, `kasm-write-query`,
  `on_query_update.sh`, `kasmvnc-no-tls.yaml`) live here, copied from 04.
- 05 talks directly to the host docker engine via the mounted socket and
  spawns its own slots.
- The compose file uses the default bridge network — no external network
  required.

## Why the parent page reads the URL, not the iframe

Two reasons to keep URL ownership on the parent:

1. **No NoVNC patching.** The iframe content is whatever kasm serves. We
   don't have to thread URL data through the NoVNC client.
2. **No cross-origin postMessage dance.** The iframe sits on a different
   port → different origin. With our flow, the parent is the single source
   of truth, the container side is a file poll (or a hook), and any
   in-desktop app can react.

## In-container helper + hook

- `/usr/local/bin/kasm-write-query` (Python) — reads JSON on stdin, writes
  both files atomically (`temp + os.replace`), shell-quotes env values
  (single-quoted so `source` survives spaces and metacharacters).
- `/dockerstartup/on_query_update.sh` — optional bash hook fired by the
  writer as a detached child. Default behaviour: reopen `KASM_Q_OPEN_URL`
  in firefox / libreoffice / vlc on change. Override in derived images.

## Server lifecycle

- On start: clean any prior containers with our
  `control-center=05` + `control-center.instance=<inst>` label pair
  (orphans from a crash), spawn the pool, wait for each slot to serve HTTP
  (concurrent polling), then begin listening on 5081.
- On SIGTERM/SIGINT/atexit: `docker rm -f` every slot. So a `docker compose
  restart` always gives you a clean pool.

## What's deferred

- Auth on 5081.
- Idle/busy slot tracking with a release signal.
- Per-tab affinity across reloads (e.g., session-id cookie).
- Multiple desktop images in the pool (chromium, vlc, libreoffice).
- Bidirectional postMessage from iframe → parent.
- Auto-reset of a slot's state between leases.
- Reconnect-on-crash for a dead slot.
