# lgtv-companion-linux

Keep LG webOS TVs in sync with your Linux PC's power state: automatic power
on/off with boot, shutdown, suspend and resume; screen blanking on user idle;
HDMI input management; Wake-on-LAN; and a CLI exposing the full webOS control
surface (picture, sound, inputs, buttons, luna settings).

A feature-parity Linux port of the excellent
[LGTV Companion](https://github.com/JPersson77/LGTVCompanion) for Windows by
Jörgen Persson — designed to work on immutable distributions such as Bazzite
(Fedora Atomic) as well as regular desktops.

## How it works

| Component | Runs as | Job |
|---|---|---|
| `lgtvc-daemon` | system service | Owns TV websocket sessions and pairing keys; reacts to logind suspend/resume/shutdown (with delay inhibitors); Wake-on-LAN; wake-on-input; user-idle blanking; IPC socket for the CLI and scripting |
| `lgtvc-agent` | user service | Session-side eyes: input activity (with mouse debounce, stick deadband, ignored keys) and MPRIS playback state — things the confined system daemon cannot see |
| `lgtvc` | CLI | The full ~115-command surface, LGTVcli-compatible syntax; talks to the daemon, or straight to the TV with `--direct` |
| `lgtvc-tray` | user service (optional) | Qt tray + settings UI (planned, v0.3) |

Reboot vs shutdown is detected **deterministically** via systemd
(`PrepareForShutdownWithMetadata` + a `Conflicts=reboot.target` fallback unit):
a reboot leaves the TV on, a shutdown powers it off. No localized-string
matching like on Windows.

Before an automatic power-off the daemon checks which input the TV is showing
(`getForegroundAppInfo`): if someone is watching another HDMI source or an
app, the TV is left alone.

## Install (system mode, works on ostree/immutable distros)

```sh
sudo python3 -m venv /usr/local/lib/lgtv-companion
sudo /usr/local/lib/lgtv-companion/bin/pip install lgtvcompanion   # or a git checkout
sudo ln -s /usr/local/lib/lgtv-companion/bin/lgtvc{,-daemon} /usr/local/bin/
sudo lgtvc setup install --mode system --service-user $USER
sudo lgtvc setup pair --host <tv-ip>       # approve the prompt on the TV
sudo systemctl enable --now lgtvc-daemon lgtvc-shutdown
systemctl --user enable --now lgtvc-agent
```

Nothing touches `/usr` (the venv lives in `/usr/local` = `/var/usrlocal` on
ostree); units live in `/etc/systemd/system`, config in
`/etc/lgtv-companion/config.json`, pairing keys in
`/var/lib/lgtv-companion/keys/`.

Migrating from Windows? `lgtvc setup import-windows --file config.json`
converts your existing settings including device session keys (no re-pairing).

## CLI

```sh
lgtvc -poweron                        # all devices
lgtvc -backlight 80 -mute LivingRoom  # several commands, device by name
lgtvc -sethdmi 2
lgtvc -picturemode filmMaker -energysaving off
lgtvc -button INFO
lgtvc -get_system_settings picture '["backlight","contrast"]'
lgtvc -output friendly -request com.webos.service.tvpower/power/getPowerState
lgtvc events                          # stream SYSTEM_* events for scripting
```

Same syntax as the Windows `LGTVcli`: commands are case-insensitive, several
per invocation, trailing bare words select devices (none = all).

## Feature parity vs upstream (Windows)

| Upstream feature | Here |
|---|---|
| Power sync: boot / shutdown / reboot / suspend / resume | ✅ via logind + systemd (reboot detection is deterministic, an improvement) |
| Wake-on-LAN (broadcast / subnet / directed / auto, multi-MAC) | ✅ |
| HDMI source-safety guard on power-off | ✅ |
| Set HDMI input on power-on (with delay) | ✅ |
| User-idle screen blank + mute, ignored keys, process list | ✅ (evdev instead of Raw Input) |
| Fullscreen / "video wake lock" idle vetoes | MPRIS playback + logind idle inhibitors; compositor fullscreen detection planned (v0.3) |
| Full settings CLI (picture/sound/inputs/ambient/service-menu/Flex) | ✅ 74 luna settings vendored from upstream, identical names and values |
| Virtual remote buttons | ✅ (pointer-input socket) |
| Multiple TVs | ✅ |
| Display-topology mode | planned (v0.3, DRM/EDID) |
| Remote-stream detection (Sunshine/Steam) | hooks now (`-streaming_connect/_disconnect`); auto-detection planned (v0.3) |
| External scripting API | ✅ unix socket, same `SYSTEM_*` event names (was: named pipe) |
| Settings GUI + tray | planned (v0.3, Qt) |
| Auto-updater (MSI) | not applicable — use pip/pipx; version check planned |
| RDP detection, NIC LUID binding, locale word lists | not applicable on Linux |

## Documentation

- [Install](docs/install.md) — system & user modes, ostree/SELinux notes
- [Migration](docs/migration.md) — from Windows or a hand-rolled setup
- [External API](docs/external-api.md) — event stream + scripting
- [Parity matrix](docs/parity.md) — vs the Windows app
- [Troubleshooting](docs/troubleshooting.md) — EACCES, WoL, suspend, and more

## License

MIT. Portions of the design and the command/button tables are derived from
[LGTV Companion](https://github.com/JPersson77/LGTVCompanion),
© Jörgen Persson, MIT.
