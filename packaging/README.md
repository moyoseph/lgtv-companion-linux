# Distro packages (.deb / .rpm / Arch)

Native packages are built by [nfpm](https://nfpm.goreleaser.com/) and attached
to every [GitHub Release](https://github.com/moyoseph/lgtv-companion-linux/releases).

## What's in them

- The app + its runtime deps (websockets, dbus-fast) vendored under
  `/opt/lgtv-companion/lib` as **pure Python** (compiled speed-ups stripped), so
  a single **noarch** package runs on any CPU arch and any Python 3.11–3.13.
- `/usr/bin/lgtvc` — the CLI (runs the system `python3` with the vendored libs).
- systemd units in `/usr/lib/systemd/{system,user}` (`lgtvc-daemon`,
  `lgtvc-shutdown`, `lgtvc-sleep`, and the user `lgtvc-agent`). Installed but
  **not enabled** — pair first.
- Only dependency: `python3 >= 3.11`.

The tray (`PySide6`) and MQTT (`aiomqtt`) extras are **not** bundled; install
those via `pip`/`pipx` if you want them.

## Install

```sh
sudo apt install ./lgtvcompanion_*_all.deb        # Debian/Ubuntu/Pop/Mint
sudo dnf install ./lgtvcompanion-*.noarch.rpm     # Fedora/RHEL/openSUSE
sudo pacman -U ./lgtvcompanion-*-any.pkg.tar.zst  # Arch/Manjaro
```

Then:

```sh
sudo lgtvc setup pair --host <tv-ip>      # approve the prompt on the TV
sudo systemctl enable --now lgtvc-daemon lgtvc-shutdown
systemctl --user enable --now lgtvc-agent
```

> **Immutable distros (Bazzite, Silverblue):** don't `rpm-ostree install` this —
> use the self-contained venv install in the [README](../README.md) instead
> (nothing touches `/usr`).

## Build locally

```sh
VERSION=0.2.1 packaging/build.sh          # needs uv + nfpm
ls dist-pkg/
```
