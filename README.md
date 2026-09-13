# lgtv-companion-linux

Make your LG (webOS) TV turn on and off with your PC. When the computer boots
the TV wakes up; when it sleeps or shuts down the TV powers off. It also blanks
the TV when you're idle, switches HDMI inputs, wakes the TV over the network,
and gives you a full command-line remote for everything else (picture, sound,
inputs, buttons).

Built for **Bazzite** and other immutable/atomic distros — it never writes to
`/usr` — but works on any Linux desktop. It's a Linux port of the excellent
[LGTV Companion](https://github.com/JPersson77/LGTVCompanion) for Windows by
Jörgen Persson.

## What it does

- 🔌 **TV follows your PC** — on at boot, off at shutdown/suspend, back on at
  resume. A reboot leaves the TV on; a real shutdown turns it off.
- 🛡️ **Won't interrupt you** — before powering the TV off it checks the active
  input. If you're watching another HDMI device (console, receiver) or a TV app,
  it leaves the TV alone.
- 😴 **Idle blanking** — blanks (or powers off) the TV after you step away and
  wakes on mouse/keyboard/controller input. Won't blank while a video is playing
  full-screen.
- 📺 **HDMI switching** — jump to your PC's input automatically on power-on.
- ⚡ **Wake-on-LAN** — wake a TV that's fully off.
- 🎮 **Streaming-aware** — turns the local TV off while you stream the desktop
  with Sunshine/Apollo (and Parsec, Moonlight, Chrome Remote Desktop, …).
- 🏠 **Home Assistant** — optional MQTT bridge with auto-discovery.
- 🕹️ **Full CLI remote** — picture, sound, inputs, luna settings, and 78 remote
  buttons, straight from the terminal or a script.
- ✅ **Works with the latest webOS** — including the 2025 firmware pairing change
  (issue #351) that stops many older remote tools from connecting.

## Install on Bazzite

The recommended setup for a couch/HTPC is **system mode**, so the TV comes on at
boot before you even log in. Paste this into a terminal:

```sh
# 1. Install into a self-contained venv (nothing touches the system /usr)
sudo python3 -m venv /usr/local/lib/lgtv-companion
sudo /usr/local/lib/lgtv-companion/bin/pip install lgtvcompanion
sudo ln -s /usr/local/lib/lgtv-companion/bin/lgtvc{,-daemon,-agent} /usr/local/bin/

# 2. Generate the systemd services (they act on the TV as your user)
sudo lgtvc setup install --mode system --service-user $USER

# 3. Pair — approve the prompt that appears on the TV screen
sudo lgtvc setup pair --host <tv-ip>

# 4. Start everything
sudo systemctl enable --now lgtvc-daemon lgtvc-shutdown
systemctl --user enable --now lgtvc-agent
```

That's it — reboot and the TV should come on with the PC.

- Want the **tray icon + settings window** or **Home Assistant**? Install
  `lgtvcompanion[tray]` or `lgtvcompanion[mqtt]` instead (line 2 above).
- Prefer a **no-root, user-only** install, or `pipx`/`uv`? See the
  [install guide](docs/install.md).
- Coming from **Windows**? `lgtvc setup import-windows --file config.json` brings
  your settings *and* pairing keys over — no re-pairing.

## Everyday commands

```sh
lgtvc -poweron                        # turn on every TV
lgtvc -poweroff LivingRoom            # …one, by name
lgtvc -sethdmi 2                      # switch input
lgtvc -backlight 80 -mute             # several commands at once
lgtvc -picturemode filmMaker          # picture / sound / energy settings
lgtvc -button INFO                    # press a remote button
lgtvc status                          # what the daemon and TVs are doing
```

Same syntax as the Windows `LGTVcli`: commands are case-insensitive, you can
chain several per line, and trailing words pick devices (none = all). Add
`--direct --host <tv-ip>` to talk to a TV without the daemon.

## How it compares to the Windows app

Full power sync, Wake-on-LAN, HDMI control, idle blanking with fullscreen/video
vetoes, the complete settings CLI, and multi-TV support are all here. A few
things are **better** on Linux (reboot-vs-shutdown is detected deterministically
via systemd, not by matching localized text) and a few are **extra** (a
first-party MQTT/Home Assistant bridge, version-update notifications). Windows-only
bits like the MSI auto-updater and RDP detection don't apply.

See the [full parity matrix](docs/parity.md) for the item-by-item breakdown.

<details>
<summary>Under the hood (the moving parts)</summary>

| Component | Runs as | Job |
|---|---|---|
| `lgtvc-daemon` | system service | Owns the TV websocket sessions and pairing keys; boot power-on, Wake-on-LAN, wake-on-input, idle blanking + vetoes, streaming/lock reactions, HDMI topology, and the IPC socket the CLI talks to. |
| `lgtvc-agent` | user service | The session-side "eyes" the confined system daemon can't have: input activity, media playback, fullscreen, screen-lock and streaming-process detection. |
| `lgtvc` | CLI | The full webOS command surface; also `status` / `reload` / `events`. |
| `lgtvc-tray` | user service *(optional)* | PySide6 tray + settings window (`[tray]` extra). |
| `lgtvc-mqtt` | system service *(optional)* | MQTT / Home Assistant bridge (`[mqtt]` extra). |

Suspend/resume and shutdown are driven by short-lived `lgtvc-sleep` /
`lgtvc-shutdown` systemd units rather than the daemon, which is how the
reboot-vs-shutdown distinction stays reliable. On ostree/immutable systems the
venv lives in `/usr/local` (`/var/usrlocal`), units in `/etc/systemd/system`,
config in `/etc/lgtv-companion/config.json`, and pairing keys in
`/var/lib/lgtv-companion/keys/`.

</details>

## Documentation

- [Install](docs/install.md) — system & user modes, ostree/SELinux notes
- [MQTT / Home Assistant](docs/mqtt.md) — bridge + auto-discovery
- [Migration](docs/migration.md) — from Windows or a hand-rolled setup
- [External API](docs/external-api.md) — event stream + scripting
- [Parity matrix](docs/parity.md) — vs the Windows app
- [Troubleshooting](docs/troubleshooting.md) — pairing, WoL, suspend, and more

## License

MIT. Portions of the design and the command/button tables are derived from
[LGTV Companion](https://github.com/JPersson77/LGTVCompanion),
© Jörgen Persson, MIT.
