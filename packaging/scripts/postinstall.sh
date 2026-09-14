#!/bin/sh
set -e
systemctl daemon-reload 2>/dev/null || true
mkdir -p /etc/lgtv-companion /var/lib/lgtv-companion
cat <<'MSG'

lgtv-companion installed. Next steps:
  1. Pair with your TV (approve the prompt on-screen):
       sudo lgtvc setup pair --host <tv-ip>
  2. Enable the services you want:
       sudo systemctl enable --now lgtvc-daemon lgtvc-shutdown   # power sync
       systemctl --user enable --now lgtvc-agent                 # idle/session
       systemctl --user enable --now lgtvc-tray                  # tray icon
       sudo systemctl enable --now lgtvc-mqtt                    # Home Assistant
  GNOME users: the tray needs an AppIndicator/Tray extension.
  Docs: https://github.com/moyoseph/lgtv-companion-linux
MSG
