#!/usr/bin/env bash
# Wrapper entrypoint baked into the dispatcher and BIND-MOUNTED into every
# spawned kasmweb/* viewer container as /usr/local/bin/kasm-no-tls-entrypoint.sh.
#
# kasmweb/* images (verified against kasmweb/chromium:1.16.0 and
# kasmweb/vlc:1.16.0) launch KasmVNC with a HARDCODED `-sslOnly` flag in
# /dockerstartup/vnc_startup.sh, AND with HTTP basic auth on / by default.
# Both produce friction in a "click-the-link-and-be-in-the-viewer" flow:
#   - `-sslOnly` makes the browser hit a self-signed cert warning.
#   - basic auth makes the browser show a username/password prompt before
#     it even runs the NoVNC client, defeating ?password=<pw>&autoconnect=1.
#
# We tested the brief's suggested env vars on these image tags:
#   KASM_SVC_HTTPS=disabled / no / false / 0 — NONE were honoured. The string
#   "KASM_SVC_HTTPS" doesn't appear anywhere in /dockerstartup, /opt, or the
#   KasmVNC binaries on the 1.16.0 images. Documented in PLAN.md.
#
# So we patch /dockerstartup/vnc_startup.sh in-place at container start:
#   - replace the literal `-sslOnly ` with `-DisableBasicAuth `. This kills
#     two birds: removes sslOnly (so KasmVNC accepts plain ws://) AND adds
#     -DisableBasicAuth so / serves the NoVNC index without an HTTP auth
#     challenge. The substitution stays inside the existing argv slot, so
#     no spacing/quoting surprises.
#   - require root to do the sed (the file is owned by root); we then drop
#     to kasm-user to run the actual entrypoint chain that the image ships.
#
# We also rely on a bind-mounted /etc/kasmvnc/kasmvnc.yaml that sets
# `network.ssl.require_ssl: false` — because KasmVNC also reads SSL
# requirement from yaml (yaml + CLI together: removing CLI alone isn't
# enough, the yaml default is `true`). See dispatcher/kasmvnc-no-tls.yaml.

set -eu

STARTUP=/dockerstartup/vnc_startup.sh

if [[ -f "$STARTUP" ]]; then
    # Replace `-sslOnly ` (with trailing space — that's how it appears in the
    # vncserver invocation, in BOTH the foreground and the kill-restart path).
    # We swap it for `-DisableBasicAuth ` which is also a flag without value,
    # so argv length is preserved.
    sed -i 's/-sslOnly /-DisableBasicAuth /g' "$STARTUP"
fi

# Hand off to the image's original entrypoint chain. We discovered this by
# `docker inspect`ing kasmweb/chromium:1.16.0 and kasmweb/vlc:1.16.0 — they
# both use the same 3-script chain.
#
# We need to drop privileges to kasm-user because:
#   - VLC refuses to run as root.
#   - the rest of KasmVNC expects $HOME to be /home/kasm-user.
# `su -p` preserves the env we set on `docker run -e ...`.
exec su -p kasm-user -c '/dockerstartup/kasm_default_profile.sh /dockerstartup/vnc_startup.sh /dockerstartup/kasm_startup.sh --wait'
