#!/usr/bin/env bash
# Optional hook fired by /usr/local/bin/kasm-write-query after every successful
# /tmp/kasm_query.{json,env} rewrite. Default behaviour: open a fresh URL in
# Firefox if KASM_Q_OPEN_URL changed.
#
# Override this script with a bind-mount or COPY in a derived image to change
# what happens when the parent control-center pushes URL params.
#
# Inputs (set by kasm-write-query):
#   KASM_QUERY_JSON  -> /tmp/kasm_query.json   (full payload, JSON)
#   KASM_QUERY_ENV   -> /tmp/kasm_query.env    (KEY=VALUE, shell-sourceable)
#
# Logs go to /tmp/on_query_update.log.
set -eu

# Source the env file so KASM_Q_* are populated.
ENV_FILE="${KASM_QUERY_ENV:-/tmp/kasm_query.env}"
[ -r "$ENV_FILE" ] || exit 0
# shellcheck disable=SC1090
. "$ENV_FILE"

# Demo: if KASM_Q_OPEN_URL changed, open it. Cache previous value in /tmp so
# we don't reopen on every keystroke.
NEW_URL="${KASM_Q_OPEN_URL:-}"
CACHE=/tmp/.on_query_update.last_open_url
PREV=""
[ -f "$CACHE" ] && PREV="$(cat "$CACHE" 2>/dev/null || true)"

if [ -n "$NEW_URL" ] && [ "$NEW_URL" != "$PREV" ]; then
    echo "$NEW_URL" > "$CACHE"
    echo "[on_query_update] open_url changed -> $NEW_URL"
    # Pick an opener by extension (mirrors ubuntu-custom-startup.sh's choices).
    lower="${NEW_URL,,}"
    case "$lower" in
        *.pdf|*.html|*.htm|*.png|*.jpg|*.jpeg|*.gif|*.webp|*.svg|*.txt|*.md|*.json|*.xml|*.csv)
            opener=firefox ;;
        *.docx|*.doc|*.xlsx|*.xls|*.pptx|*.ppt|*.odt|*.ods|*.odp|*.rtf)
            opener=libreoffice ;;
        *.mp4|*.mkv|*.webm|*.mov|*.avi|*.mp3|*.wav|*.flac|*.ogg|*.m4a)
            opener=vlc ;;
        *)  opener=xdg-open ;;
    esac
    command -v "$opener" >/dev/null 2>&1 || opener=xdg-open
    # Background — don't block the writer.
    DISPLAY="${DISPLAY:-:1}" nohup "$opener" "$NEW_URL" \
        >/tmp/xdg-open.log 2>&1 &
fi

exit 0
