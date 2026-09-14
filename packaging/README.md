# Distro packages (.deb / .rpm / Arch)

Native packages are built by [nfpm](https://nfpm.goreleaser.com/) and attached
to every [GitHub Release](https://github.com/moyoseph/lgtv-companion-linux/releases).

## What's in them

- The app + its runtime deps (websockets, dbus-fast, **and the MQTT bridge —
  aiomqtt/paho**) vendored under `/opt/lgtv-companion/lib` as **pure Python**
  (compiled speed-ups stripped), so a single **noarch** package runs on any CPU
  arch and any Python 3.11–3.13.
- `/usr/bin/lgtvc`, `/usr/bin/lgtvc-tray`, `/usr/bin/lgtvc-mqtt` launchers.
- All systemd units in `/usr/lib/systemd/{system,user}`: `lgtvc-daemon`,
  `lgtvc-shutdown`, `lgtvc-sleep`, `lgtvc-mqtt` (system) and `lgtvc-agent`,
  `lgtvc-tray` (user). Installed but **not enabled** — pair first.
- A themed tray icon at `/usr/share/icons/hicolor/scalable/apps/`.
- Dependency: `python3 >= 3.11`. The **tray** needs PySide6:
  - **Fedora / Arch** package it, so it's a hard dependency — the tray works out
    of the box.
  - **Debian / Ubuntu** don't ship PySide6. The rest installs fine; to use the
    tray, add PySide6 into the app's lib once:
    ```sh
    sudo python3 -m pip install --target /opt/lgtv-companion/lib PySide6
    ```
  The tray uses the StatusNotifierItem protocol, so it works on KDE, XFCE, MATE,
  Cinnamon, Budgie, etc.

> **GNOME** removed the legacy tray — install a *Tray/AppIndicator* extension
> (e.g. "AppIndicator and KStatusNotifierItem Support") for the icon to appear.
> The tray unit self-skips where no tray host is present, and gives up after a
> few failed starts (e.g. if PySide6 isn't installed).

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
