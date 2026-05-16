# 05 — verification log

## Build + bring up

```
$ docker build -t kasm2-cc/ubuntu:latest images/   # one-off
$ docker compose up -d --build
$ until [ "$(curl -sS http://localhost:5081/healthz | jq -r .pool_ready)" = "true" ]; do sleep 3; done
$ curl -sS http://localhost:5081/healthz | jq
{
  "ok": true,
  "pool_size": 3,
  "pool_ready": true,
  "pool_image": "kasm2-cc/ubuntu:latest",
  "pool_instance": "default",
  "viewer_port_range": "5082-5084",
  "kasm_https": "disabled",
  "public_host_override": null,
  "slots": [
    {"idx": 0, "name": "kasm-cc-default-0", "host_port": 5082},
    {"idx": 1, "name": "kasm-cc-default-1", "host_port": 5083},
    {"idx": 2, "name": "kasm-cc-default-2", "host_port": 5084}
  ]
}
```

## Round-robin lease

Four `POST /api/session` requests in a row → slots 0, 1, 2, 0 (rotates).
Confirmed via `slot` field in response.

## E2E (agent-browser)

```
$ agent-browser open 'http://HOST:5081/?desktop=ubuntu&theme=dark&user=milan'
$ agent-browser wait '#viewer[src]' 60000
$ agent-browser get text '#status-state'    → connected
$ agent-browser get text '#status-session'  → session: 04364813e9fa
$ agent-browser get text '#status-params'   → params: {"theme":"dark","user":"milan"}
$ agent-browser get value '#url-input'      → http://HOST:5081/?desktop=ubuntu&theme=dark&user=milan
```

`docker exec 04364813e9fa cat /tmp/kasm_query.json`:

```json
{ "params": { "theme": "dark", "user": "milan" },
  "raw":    "theme=dark&user=milan" }
```

## Hot-patch via navbar URL input

```
$ agent-browser fill '#url-input' 'http://HOST:5081/?desktop=ubuntu&theme=dark&user=milan&note=hello+world'
$ agent-browser press Enter
```

Within ~1.5s (poll + debounce):

```
$ agent-browser get text '#status-params'
params: {"theme":"dark","user":"milan","note":"hello world"}

$ docker exec 04364813e9fa cat /tmp/kasm_query.json
{ "params": { "theme": "dark", "user": "milan", "note": "hello world" },
  "raw":    "theme=dark&user=milan&note=hello+world" }

$ docker exec 04364813e9fa cat /tmp/kasm_query.env
KASM_Q_THEME='dark'
KASM_Q_USER='milan'
KASM_Q_NOTE='hello world'
KASM_QUERY='theme=dark&user=milan&note=hello+world'
KASM_QUERY_KEYS='theme user note'

$ docker exec 04364813e9fa sh -c '. /tmp/kasm_query.env && \
    echo "theme=[$KASM_Q_THEME] note=[$KASM_Q_NOTE] keys=[$KASM_QUERY_KEYS]"'
theme=[dark] note=[hello world] keys=[theme user note]
```

Screenshots:
- `screenshots/01-spawn-theme-dark.png`         initial pool lease
- `screenshots/02-after-add-user.png`           first hot-patch
- `screenshots/03-second-mutate.png`            second hot-patch
- `screenshots/04-url-input.png`                navbar URL-input edit
- `screenshots/06-pool-hotpatch.png`            pool slot hot-patch via input

## Open real software (hook demo)

```
$ agent-browser fill '#url-input' \
    'http://HOST:5081/?desktop=ubuntu&open_url=https%3A%2F%2Fwww.w3.org%2FWAI%2FER%2Ftests%2Fxhtml%2Ftestfiles%2Fresources%2Fpdf%2Fdummy.pdf'
$ agent-browser press Enter
```

`docker exec <slot> cat /tmp/on_query_update.log`:

```
[on_query_update] open_url changed -> https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf
```

Firefox launches inside the desktop and renders the PDF. The hook caches
the last value in `/tmp/.on_query_update.last_open_url` so repeated identical
URLs don't relaunch.

## Result

- ✅ Pool initialises and is ready before listening (3 slots, ports 5082-5084).
- ✅ Round-robin lease distributes across slots.
- ✅ Initial params persisted in `/tmp/kasm_query.json` on lease.
- ✅ Address-bar mutate hot-patches within ~1.5s.
- ✅ Navbar URL-input edit pushes via `history.replaceState` and round-trips.
- ✅ `/tmp/kasm_query.env` survives `source` with whitespace values.
- ✅ `open_url=<file>` triggers the `on_query_update.sh` hook and opens the
  file in the right app inside the desktop.
- ✅ No dependency on the 04 stack — `docker compose down` 04 keeps 05 running.
