#!/usr/bin/env bash
# Build .deb / .rpm / Arch packages with nfpm.
#
# Vendors lgtvcompanion + its runtime deps (websockets, dbus-fast) into
# /opt/lgtv-companion/lib and ships static systemd units that run the system
# python3 with `-m`. The compiled speed-up extensions are stripped so the
# payload is pure Python — one noarch package that works on any CPU arch and
# any python 3.11–3.13 (websockets/dbus-fast fall back to pure Python).
#
# Requires: uv, nfpm.  Usage:  VERSION=0.2.1 packaging/build.sh [path/to/wheel]
set -euo pipefail

: "${VERSION:?set VERSION (e.g. VERSION=0.2.1)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LIB="build/pkgroot/opt/lgtv-companion/lib"
rm -rf build/pkgroot dist-pkg
mkdir -p "$LIB" dist-pkg

WHEEL="${1:-}"
if [ -z "$WHEEL" ]; then
  uv build --wheel >/dev/null
  WHEEL="$(ls -1 dist/lgtvcompanion-*-py3-none-any.whl | sort | tail -1)"
fi
echo "vendoring $WHEEL + runtime deps -> $LIB"
uv pip install --quiet --target "$LIB" "$WHEEL"

# Make the payload portable pure-Python: drop compiled extensions, caches, bin/
find "$LIB" -name '*.so' -delete
find "$LIB" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$LIB" -name 'uv_cache.json' -delete
rm -rf "$LIB"/bin "$LIB"/.lock

export VERSION
for pkg in deb rpm archlinux; do
  echo "== nfpm $pkg =="
  nfpm package --config packaging/nfpm.yaml --packager "$pkg" --target dist-pkg/
done

echo "built:"
ls -1 dist-pkg/
