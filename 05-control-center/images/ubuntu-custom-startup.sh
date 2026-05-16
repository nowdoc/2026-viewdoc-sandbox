#!/usr/bin/env bash
# Optional URL opener for kasm2/ubuntu-notls. Runs as a backgrounded child of
# vnc_startup.sh — so it must not block the session, and is allowed to exit
# after the URL is open.
#
# Triggered when the dispatcher sets $LAUNCH_URL (or KASM_URL). If unset, this
# script is a no-op: the user just gets a plain Ubuntu desktop.
#
# Also publishes user-supplied query params from the dispatcher URL:
#   - environment: every KASM_Q_<KEY> env var is already present here.
#   - on-disk: /tmp/kasm_query.env (KEY=value lines, shell-sourceable) and
#     /tmp/kasm_query.json so any app inside the desktop can read them.
set -eu

# Publish KASM_Q_*/KASM_QUERY to a couple of well-known files. Apps started
# later (clicking a launcher in xfce, opening a terminal, etc.) inherit the
# env from the parent session, but some workflows are easier with a file —
# e.g. `source /tmp/kasm_query.env` from a script.
{
    env | grep -E '^KASM_Q_|^KASM_QUERY' | sort
} > /tmp/kasm_query.env 2>/dev/null || true
python3 - <<'PY' >/tmp/kasm_query.json 2>/dev/null || true
import json, os
out = {}
for k, v in os.environ.items():
    if k.startswith("KASM_Q_"):
        out[k[len("KASM_Q_"):].lower()] = v
print(json.dumps({"params": out, "raw": os.environ.get("KASM_QUERY", "")}, indent=2))
PY
chmod 644 /tmp/kasm_query.env /tmp/kasm_query.json 2>/dev/null || true

URL="${KASM_URL:-${LAUNCH_URL:-}}"
[ -n "$URL" ] || { echo "kasm2: no LAUNCH_URL/KASM_URL — desktop only"; exit 0; }

# Wait for desktop ready (xfce + window manager up). /usr/bin/desktop_ready
# is shipped by the kasm image; it blocks until the xfce session is mature.
if [ -x /usr/bin/desktop_ready ]; then
    /usr/bin/desktop_ready
fi

# Pre-download HTTP(S) URLs to a local file. Most desktop apps (xdg-open
# delegate, image viewer, video player) prefer file paths anyway, and it
# avoids relying on per-app HTTP support.
if [[ "$URL" =~ ^https?:// ]]; then
    base="${URL##*/}"
    base="${base%%\?*}"
    case "$base" in
        *.*) ext="${base##*.}" ;;
        *)   ext=bin ;;
    esac
    LOCAL="/tmp/payload.${ext}"
    echo "kasm2: downloading $URL -> $LOCAL"
    if curl -fsSL --retry 3 -o "$LOCAL" "$URL"; then
        TARGET="$LOCAL"
    else
        echo "kasm2: download failed; opening URL directly"
        TARGET="$URL"
    fi
else
    TARGET="$URL"
fi

# Pick an opener. xdg-open consults MIME associations, which on the
# kasmweb/ubuntu-jammy-desktop image map PDF to GIMP (because GIMP is
# installed and registers as a PDF handler). For common viewable types we
# prefer Firefox — it has a built-in PDF reader, opens images natively, and
# is preinstalled. Office docs go to LibreOffice when available.
case "${TARGET,,}" in
    *.pdf|*.html|*.htm|*.png|*.jpg|*.jpeg|*.gif|*.webp|*.svg|*.bmp|*.txt|*.md|*.json|*.xml|*.csv)
        OPENER=firefox ;;
    *.docx|*.doc|*.xlsx|*.xls|*.pptx|*.ppt|*.odt|*.ods|*.odp|*.rtf)
        OPENER=libreoffice ;;
    *.mp4|*.mkv|*.webm|*.mov|*.avi|*.mp3|*.wav|*.flac|*.ogg|*.m4a)
        OPENER=vlc ;;
    *)
        OPENER=xdg-open ;;
esac

# Fall back to xdg-open if the preferred opener isn't installed.
command -v "$OPENER" >/dev/null 2>&1 || OPENER=xdg-open

echo "kasm2: $OPENER $TARGET"
nohup "$OPENER" "$TARGET" >/tmp/xdg-open.log 2>&1 &
exit 0
