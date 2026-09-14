#!/bin/sh
set -e
systemctl daemon-reload 2>/dev/null || true
mkdir -p /etc/lgtv-companion /var/lib/lgtv-companion
cat <<'MSG'

lgtv-companion installed. Next steps:
  1. Pair with your TV (approve the prompt on-screen):
       sudo lgtvc setup pair --host <tv-ip>
  2. Enable the services:
       sudo systemctl enable --now lgtvc-daemon lgtvc-shutdown
       systemctl --user enable --now lgtvc-agent
  Docs: https://github.com/moyoseph/lgtv-companion-linux
MSG
