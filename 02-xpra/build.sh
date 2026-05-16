#!/usr/bin/env bash
# Build both images used by the prototype.
#   - xpra-viewer:latest  (Debian + xpra + xpra-html5 + xpdf/feh/mpv)
#   - xpra-dispatcher:latest (Python stdlib HTTP service + docker CLI)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[build] xpra-viewer:latest"
docker build -t xpra-viewer:latest -f "$HERE/images/Dockerfile.viewer" "$HERE/images"

echo "[build] xpra-dispatcher:latest"
docker build -t xpra-dispatcher:latest -f "$HERE/dispatcher/Dockerfile" "$HERE/dispatcher"

echo "[build] done"
