#!/usr/bin/env bash
# Fired by /usr/local/bin/webtop-write-query after every successful write to
# /tmp/webtop_query.{json,env}. Default behaviour: if WEBTOP_Q_OPEN_URL
# changed, hand it off to vlc (for known media extensions) or xdg-open
# (everything else, which Firefox handles for web URLs and PDFs).
#
# Inputs (set by webtop-write-query):
#   WEBTOP_QUERY_JSON  -> /tmp/webtop_query.json   (full payload, JSON)
#   WEBTOP_QUERY_ENV   -> /tmp/webtop_query.env    (KEY=VALUE, shell-sourceable)
#
# Logs go to /tmp/on_query_update.log.
set -eu

# docker exec gives us a bare env; populate the basics the desktop session
# expects. Selkies inside the LSIO webtop puts the X server on :0.
: "${DISPLAY:=:0}"
: "${HOME:=/config}"
export DISPLAY HOME

ENV_FILE="${WEBTOP_QUERY_ENV:-/tmp/webtop_query.env}"
[ -r "$ENV_FILE" ] || exit 0
# shellcheck disable=SC1090
. "$ENV_FILE"

NEW_URL="${WEBTOP_Q_OPEN_URL:-}"
CACHE=/tmp/.on_query_update.last_open_url
PREV=""
[ -f "$CACHE" ] && PREV="$(cat "$CACHE" 2>/dev/null || true)"

if [ -n "$NEW_URL" ] && [ "$NEW_URL" != "$PREV" ]; then
    echo "$NEW_URL" > "$CACHE"
    echo "[on_query_update] open_url -> $NEW_URL"
    lower="${NEW_URL,,}"
    case "$lower" in
        *.mp4|*.mkv|*.webm|*.mov|*.avi|*.mp3|*.wav|*.flac|*.ogg|*.m4a)
            opener=vlc ;;
        *)
            opener=xdg-open ;;
    esac
    command -v "$opener" >/dev/null 2>&1 || opener=xdg-open
    nohup "$opener" "$NEW_URL" >/tmp/xdg-open.log 2>&1 &
fi

exit 0
