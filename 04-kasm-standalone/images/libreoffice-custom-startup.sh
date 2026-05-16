#!/usr/bin/env bash
# Replacement for /dockerstartup/custom_startup.sh in the kasm2/libreoffice-notls
# wrapper. LibreOffice can't open HTTP(S) URLs directly, so we download the
# asset to /tmp/payload.<ext> first, then hand the local path to libreoffice.
#
# Keeps the supervisor-loop semantics of the upstream script: if libreoffice
# crashes, it gets relaunched.
set -ex

LD_LIBRARY_PATH=:/usr/lib/libreoffice/program:/usr/lib/$(arch)-linux-gnu/
START_COMMAND="libreoffice"
PGREP="soffice.bin"
export MAXIMIZE="true"
export MAXIMIZE_NAME="LibreOffice"
MAXIMIZE_SCRIPT=$STARTUPDIR/maximize_window.sh
DEFAULT_ARGS=""
ARGS=${APP_ARGS:-$DEFAULT_ARGS}

# Resolve a URL to a local file if needed.
# Accepts $LAUNCH_URL (kasm-standalone dispatcher) or $KASM_URL (KASM
# Workspaces). Local paths are passed through unchanged.
URL=""
if [ -n "${KASM_URL:-}" ]; then
    URL="$KASM_URL"
elif [ -n "${LAUNCH_URL:-}" ]; then
    URL="$LAUNCH_URL"
fi

if [[ "$URL" =~ ^https?:// ]]; then
    base="${URL##*/}"
    base="${base%%\?*}"
    case "$base" in
        *.*) ext="${base##*.}" ;;
        *)   ext=bin ;;
    esac
    LOCAL="/tmp/payload.${ext}"
    if ! [ -s "$LOCAL" ]; then
        echo "kasm2: downloading $URL -> $LOCAL"
        curl -fsSL --retry 3 -o "$LOCAL" "$URL" || {
            echo "kasm2: download failed; libreoffice will open empty"
            LOCAL=""
        }
    fi
    URL="$LOCAL"
fi

echo "kasm2: libreoffice will open: '$URL'"

if [ -z "${DISABLE_CUSTOM_STARTUP:-}" ]; then
    echo "Entering process startup loop"
    set +x
    while true; do
        if ! pgrep -x "$PGREP" > /dev/null; then
            /usr/bin/filter_ready
            /usr/bin/desktop_ready
            set +e
            bash "$MAXIMIZE_SCRIPT" &
            $START_COMMAND $ARGS $URL
            set -e
        fi
        sleep 1
    done
    set -x
fi
