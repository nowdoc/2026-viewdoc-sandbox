#!/bin/bash
# Entrypoint: download $TARGET_URL, then launch $VIEWER under xpra HTML5 server.
set -eu

: "${TARGET_URL:?TARGET_URL is required}"
: "${VIEWER:?VIEWER is required (one of: xpdf, feh, vlc, mpv)}"

# For images/PDFs the viewer expects a local file (feh/xpdf don't speak HTTP);
# for audio/video mpv & vlc are perfectly happy to stream the URL directly, and
# downloading first would block xpra startup for large files (e.g. 250MB MP4).
case "$VIEWER" in
  vlc|mpv)
    # Streaming viewers: pass URL straight through, no download step.
    TARGET="${TARGET_URL}"
    ;;
  xpdf|feh)
    # Local-file viewers: download first.
    EXT="${TARGET_URL##*.}"
    EXT="${EXT%%\?*}"
    EXT="${EXT%%#*}"
    case "$EXT" in
      pdf|png|jpg|jpeg|gif|webp|bmp|tiff) ;;
      *) EXT="bin" ;;
    esac
    TARGET="/tmp/payload.${EXT}"
    echo "[entrypoint] downloading ${TARGET_URL} -> ${TARGET}"
    curl -fsSL --max-time 120 -o "${TARGET}" "${TARGET_URL}"
    echo "[entrypoint] download done ($(stat -c%s "${TARGET}") bytes)"
    ;;
esac

# Build viewer command. mpv/vlc accept URL or path equally.
case "$VIEWER" in
  vlc)
    VIEWER_CMD="vlc --no-qt-privacy-ask --no-qt-error-dialogs --intf qt --play-and-exit ${TARGET}"
    ;;
  mpv)
    VIEWER_CMD="mpv --force-window=yes --no-config ${TARGET}"
    ;;
  xpdf)
    VIEWER_CMD="xpdf -z page ${TARGET}"
    ;;
  feh)
    VIEWER_CMD="feh --auto-zoom --geometry 1024x768 ${TARGET}"
    ;;
  *)
    echo "[entrypoint] unknown VIEWER: $VIEWER" >&2
    exit 2
    ;;
esac

echo "[entrypoint] launching: xpra start :100 --start='${VIEWER_CMD}' --bind-tcp=0.0.0.0:14500 --tcp-auth=none --ws-auth=none --html=on --daemon=no"

# tcp-auth=none / ws-auth=none disable password (no token required).
# xpra 3.x serves the HTML5 client and websocket upgrade on the same --bind-tcp port when --html=on.
# Use --start (no exit-with-children) so xpra stays up even if the viewer crashes/quits.
exec xpra start :100 \
    --start="${VIEWER_CMD}" \
    --bind-tcp=0.0.0.0:14500 \
    --tcp-auth=none \
    --ws-auth=none \
    --html=on \
    --daemon=no \
    --no-mdns \
    --no-pulseaudio \
    --no-notifications \
    --no-systemd-run \
    --start-new-commands=no
