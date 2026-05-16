#!/usr/bin/env bash
set -euo pipefail

# Required env vars:
#   TARGET_URL — file URL to fetch & display
#   VIEWER     — one of: xpdf | feh | vlc
# Optional:
#   FILE_EXT   — filename extension (default: derived crudely from URL)

: "${TARGET_URL:?TARGET_URL is required}"
: "${VIEWER:?VIEWER is required}"

FILE_EXT="${FILE_EXT:-bin}"
TARGET_FILE="/tmp/file.${FILE_EXT}"

echo "[viewer] downloading ${TARGET_URL} -> ${TARGET_FILE}"
curl -fsSL --max-time 30 -o "${TARGET_FILE}" "${TARGET_URL}" || {
    echo "[viewer] download failed, continuing so VNC stays up"
}

# Start X virtual framebuffer
Xvfb :0 -screen 0 1280x800x24 &
XVFB_PID=$!
sleep 0.5

# Start VNC server bound to display :0
x11vnc -display :0 -forever -shared -nopw -rfbport 5900 -bg -quiet -noxdamage

# Give x11vnc a moment
sleep 0.3

# Launch viewer app
case "${VIEWER}" in
    xpdf)
        exec xpdf -fullscreen "${TARGET_FILE}"
        ;;
    feh)
        exec feh --fullscreen --auto-zoom "${TARGET_FILE}"
        ;;
    vlc)
        exec vlc --no-qt-privacy-ask --no-qt-error-dialogs --intf qt --fullscreen --loop "${TARGET_FILE}"
        ;;
    *)
        echo "[viewer] unknown VIEWER=${VIEWER}"
        # Keep container alive so VNC stays reachable
        wait $XVFB_PID
        ;;
esac
