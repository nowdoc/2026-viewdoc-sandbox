# 2026-viewdoc-sandbox

Four self-contained prototypes of a **URL-to-viewer dispatcher**: a tiny HTTP
service that takes `GET /?url=<asset-url>`, classifies the asset by extension,
spawns an ephemeral container with the right viewer, and 302-redirects the
browser into the live session — **no password prompt anywhere**.

Each prototype is a different stack (transport + spawner + viewer image
catalog). They live side-by-side so they can be compared head-to-head on the
same LAN with the same test assets. See [`COMPARISON.md`](COMPARISON.md) for
the side-by-side scorecard and recommendation.

## Prototypes

| Folder | Stack | Transport | Default ports |
|---|---|---|---|
| [`01-neko-rooms/`](01-neko-rooms/) | neko + neko-rooms REST API | WebRTC | `8080`, `8081` |
| [`02-xpra/`](02-xpra/) | xpra + Debian xpra-html5 | WebSocket | `9081`, `9082-9099` |
| [`03-guacamole/`](03-guacamole/) | Apache Guacamole (guacd + Tomcat) + json-auth | HTML5 canvas → VNC | `7080`, `7081` |
| [`04-kasm-standalone/`](04-kasm-standalone/) | KasmVNC standalone (no orchestrator) | KasmVNC (JPEG/QOI) | `6081`, `6082-6099` |

Every folder ships:

- `docker-compose.yml` — the stack
- `.env.example` — per-host overrides (copy to `.env`)
- `dispatcher/` — small Python service that classifies + spawns + redirects
- `PLAN.md` — architecture diagram and design notes
- `VERIFY.md` — manual test log with screenshots
- `README.md` — quick-start for that prototype
- `screenshots/` — end-to-end validation captures

## Quick start

Pick a prototype, then:

```bash
cd 01-neko-rooms                 # or any other folder
cp .env.example .env             # edit PUBLIC_HOST to this host's LAN IP
docker compose up -d --build
```

Then from any browser on the LAN:

```
http://<PUBLIC_HOST>:<DISPATCHER_PORT>/?url=https://example.com/foo.pdf
```

The four prototypes are designed to coexist on one host — port ranges don't
overlap.

## What's intentionally **not** here

These are MVPs aimed at evaluating the user-visible experience. None of them
are production-ready. Still owed before a real roll-out:

- session sweeper / TTL on idle containers
- per-session resource caps (`--memory --cpus`)
- concurrency cap on the dispatcher
- HTTPS termination (Caddy / Traefik) in front
- auth on the dispatcher itself
- presigned-URL workflow + CORS for non-public assets

## License

[MIT](LICENSE).
