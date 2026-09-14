#!/bin/sh
set -e
# stop + disable the system units before the files vanish (best-effort)
systemctl disable --now lgtvc-daemon lgtvc-shutdown lgtvc-sleep 2>/dev/null || true
