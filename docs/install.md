# Installing lgtv-companion-linux

Two install modes: **system** (a machine-wide service, best for an HTPC) and
**user** (per-login, no root). System mode is the default and what the examples
below use.

## System mode (works on immutable / ostree distros like Bazzite)

Nothing is written to `/usr`. The virtualenv lives in `/usr/local/lib`
(`/var/usrlocal` on ostree, which is mutable), units go in
`/etc/systemd/system`, config in `/etc/lgtv-companion/`, and pairing keys in
`/var/lib/lgtv-companion/keys/`.

```sh
sudo python3 -m venv /usr/local/lib/lgtv-companion
sudo /usr/local/lib/lgtv-companion/bin/pip install lgtvcompanion   # or: pip install <checkout>
sudo ln -s /usr/local/lib/lgtv-companion/bin/lgtvc{,-daemon,-agent} /usr/local/bin/

sudo lgtvc setup install --mode system --service-user "$USER"
sudo lgtvc setup pair --host <tv-ip>        # approve the prompt on the TV
sudo systemctl enable --now lgtvc-daemon lgtvc-shutdown lgtvc-sleep
systemctl --user enable --now lgtvc-agent   # input/MPRIS reporting (desktop + Game Mode)
```

> **ostree/SELinux note:** the systemd units launch the daemon and CLI with
> `<venv>/bin/python3 -m lgtvcompanion.…`, **not** the `lgtvc-daemon`/`lgtvc`
> console-script wrappers. Exec'ing the `lib_t`-labelled console scripts leaves
> the service in a domain where every outbound socket connect fails with
> `EACCES`. `lgtvc setup install` already generates the units correctly — don't
> hand-edit them back to the console scripts. See `docs/troubleshooting.md`.

## User mode (laptops / no root)

```sh
pipx install lgtvcompanion            # or: uv tool install lgtvcompanion
lgtvc setup install --mode user       # (planned) — meanwhile use --host ad-hoc:
lgtvc --host <tv-ip> --key '' -poweron   # first run prompts on the TV, saves the key
loginctl enable-linger "$USER"        # so the user daemon runs without a login session
```

Config: `~/.config/lgtv-companion/config.json`; keys:
`~/.local/state/lgtv-companion/keys/`.

## The tray / settings GUI

The tray needs **PySide6** (Qt for Python). How you get it depends on install
method and distro:

- **pip/venv install:** `pip install 'lgtvcompanion[tray]'` pulls PySide6 in.
- **Distro package on Fedora / Arch:** PySide6 is a hard dependency — already
  installed, nothing to do.
- **Distro package on Ubuntu / Debian:** these **do not package PySide6** (they
  ship PyQt6 instead), so add it into the app's lib once:
  ```sh
  sudo python3 -m pip install --target /opt/lgtv-companion/lib PySide6
  ```

Then enable it:

```sh
systemctl --user enable --now lgtvc-tray
```

### GNOME

GNOME dropped the built-in system tray. Install **and enable** a tray extension
or the icon won't show:

```sh
sudo dnf install gnome-shell-extension-appindicator     # Fedora
sudo apt install gnome-shell-extension-appindicator     # Ubuntu/Debian
sudo pacman -S gnome-shell-extension-appindicator       # Arch (or from AUR)
```

then turn on *"AppIndicator and KStatusNotifierItem Support"* in the Extensions
app and re-log. KDE, XFCE, MATE, Cinnamon and Budgie provide a tray natively —
nothing extra needed.

The tray self-skips where there's no StatusNotifier host (e.g. gamescope Game
Mode) and gives up after a few failed starts if PySide6 is missing. Everything
the GUI does is also available from `lgtvc` and the config file, so headless
boxes lose nothing.

## Verify

```sh
lgtvc status          # daemon + device state
lgtvc -poweron
lgtvc -poweroff
journalctl -u lgtvc-daemon -f
```
