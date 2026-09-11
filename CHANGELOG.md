# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

First public pre-release. A feature-parity Linux port of
[LGTV Companion](https://github.com/JPersson77/LGTVCompanion) for Windows,
designed to run on immutable/ostree distros (Bazzite) and regular desktops.

### Added
- **Daemon** (`lgtvc-daemon`): owns TV SSAP websocket sessions and pairing
  keys, boot power-on, Wake-on-LAN, wake-on-input, user-idle blanking, and a
  Unix-socket IPC API. Power operations are coalesced; WoL bursts throttled.
- **Power transitions** via systemd oneshot units: `lgtvc-sleep`
  (suspend/resume, `sleep.target`) and `lgtvc-shutdown` (shutdown-not-reboot,
  `Conflicts=reboot.target` — deterministic reboot detection).
- **Session agent** (`lgtvc-agent`): evdev input activity (mouse debounce,
  stick deadband, accelerometer exclusion, ignored keys), MPRIS playback, and
  KWin fullscreen — reported to the daemon for idle vetoes and wake-on-input.
- **CLI** (`lgtvc`): LGTVcli-compatible token syntax; 74 luna settings + 76
  buttons vendored from upstream; `--direct` mode; `-output default|friendly|key`;
  `events` stream; `status`; `setup` subcommands
  (install / pair / import-legacy / import-windows / migrate-legacy /
  rollback-legacy / map-display).
- **Tray + settings GUI** (`lgtvc-tray`, `[tray]` extra, PySide6): per-device
  power/blank actions, idle controls, and a settings dialog that edits config
  and hot-reloads the daemon.
- **Idle engine + veto framework**: process control list, MPRIS, logind idle
  inhibitors, KWin fullscreen.
- **Display topology** (DRM/EDID) and **Sunshine** remote-stream detection.
- Config importers from a hand-rolled `/etc/lgtvcontrol` setup and from a
  Windows `config.json` (session keys carried over — no re-pairing).
- Fake-webOS-TV test server and a suite of ~86 tests.

### Notes / known differences
- systemd units launch the daemon/CLI via `python -m`, **not** the pip
  console-script wrappers, to avoid an EACCES-on-connect interaction under
  ostree+SELinux (see `docs/troubleshooting.md`).
- Power-transition `SYSTEM_*` events are not emitted on the IPC bus (the
  oneshot units own those transitions); idle events are.
- Steam Remote Play auto-detection and a GUI-less user-mode `setup install` are
  planned.
