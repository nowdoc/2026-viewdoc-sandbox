# Thin wrapper around the stock neko firefox image that adds
#   LAUNCH_URL env-var support: whatever URL we pass via $LAUNCH_URL is
#   appended to the firefox argv, so the browser opens that page on start.
#
# Why this exists: the upstream m1k1o/neko firefox image hard-codes the
# firefox command in /etc/neko/supervisord/firefox.conf and has no env
# hook for a start URL. We rewrite that supervisord program to call a
# tiny shell wrapper that does the env interpolation.
#
# Also: we flip the policies.json so the built-in PDF viewer is enabled
# (the upstream image disables it).

FROM ghcr.io/m1k1o/neko/firefox:latest

USER root

# Shell wrapper that the supervisord program will exec. It honours the
# LAUNCH_URL env var the dispatcher injects via neko-rooms' RoomSettings.envs.
RUN cat >/usr/local/bin/firefox-launch <<'SH' && chmod +x /usr/local/bin/firefox-launch
#!/bin/sh
set -e
# Single-shot launch. If LAUNCH_URL is empty firefox just opens its homepage.
exec /usr/bin/firefox \
    --no-remote \
    -P default \
    --display="${DISPLAY}" \
    -setDefaultBrowser \
    -width 1280 \
    -height 720 \
    ${LAUNCH_URL:+"$LAUNCH_URL"}
SH

# Replace the supervisord program so it execs our wrapper instead of the
# bare firefox command. We swap the whole file rather than sed-patching
# because the upstream supervisord.conf is short and stable.
RUN cat >/etc/neko/supervisord/firefox.conf <<'CONF'
[program:firefox]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s",LAUNCH_URL="%(ENV_LAUNCH_URL)s"
command=/usr/local/bin/firefox-launch
stopsignal=INT
autorestart=true
priority=800
user=%(ENV_USER)s
stdout_logfile=/var/log/neko/firefox.log
stdout_logfile_maxbytes=100MB
stdout_logfile_backups=10
redirect_stderr=true

[program:openbox]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s"
command=/usr/bin/openbox --config-file /etc/neko/openbox.xml
autorestart=true
priority=300
user=%(ENV_USER)s
stdout_logfile=/var/log/neko/openbox.log
stdout_logfile_maxbytes=100MB
stdout_logfile_backups=10
redirect_stderr=true
CONF

# Enable Firefox's built-in PDF viewer (upstream disables it) and remove
# the force-installed SponsorBlock + uBlock extensions, which open their
# own onboarding tab on first run and obscure our LAUNCH_URL tab.
RUN python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/usr/lib/firefox/distribution/policies.json")
if p.exists():
    data = json.loads(p.read_text())
    pol = data.setdefault("policies", {})
    pol["DisableBuiltinPDFViewer"] = False
    pol["ExtensionSettings"] = {"*": {"installation_mode": "blocked"}}
    p.write_text(json.dumps(data, indent=2))
PY

# Make sure LAUNCH_URL has a default so supervisord's %(ENV_LAUNCH_URL)s
# expansion doesn't blow up when the dispatcher omits it (it always sets
# it for our use case, but defensiveness is cheap).
ENV LAUNCH_URL=""

# DO NOT add `USER neko` here — the upstream image runs as root and lets
# supervisord drop privileges to the neko user per-program. Switching to
# `neko` at image level breaks supervisord with
#   "Error: Can't drop privilege as nonroot user".
