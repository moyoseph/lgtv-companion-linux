#!/bin/sh
set -e
# stop + disable the units before the files vanish (best-effort)
systemctl disable --now lgtvc-daemon lgtvc-shutdown lgtvc-sleep lgtvc-mqtt 2>/dev/null || true
