# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

_Nothing yet._

## [0.2.0] — 2026-09-12

Feature release addressing the remaining gaps and several upstream requests.

### Added
- **User-mode install** (`lgtvc setup install --mode user`): all components as
  `--user` systemd units under XDG paths, no root; the daemon handles
  suspend/resume/shutdown itself via logind (`daemon_power_events`).
- **MQTT bridge / Home Assistant** already shipped in 0.1.0; 0.2.0 adds a cached
  per-device `power_state` to `lgtvc status` (drives the HA power sensor).
- **Idle → power-off** option (`idle.action: blank | power_off`).
- **Version-check notification** (`update_check: notify`): the agent checks the
  GitHub releases API daily and posts a desktop notification when a newer
  release exists (never downloads).
- **Cross-subnet / VPN Wake-on-LAN**: per-device `wol_targets` (explicit
  broadcast/unicast addresses) and `interface` (bind WoL to a NIC).
- `dolbyHdrFilmMaker` added to `-picturemode` (#287).

### Changed / fixed
- Wired up previously-inert config: `topology.keep_on_boot` (persist + restore
  the last topology), per-device `interface`, and `idle.veto_mpris=foreground_only`
  (veto only when playback is in a fullscreen app).
- `ipc.default_socket()` resolves the system vs `$XDG_RUNTIME_DIR` socket so the
  CLI/agent/tray/mqtt find the daemon in either install mode.
- Docs: streaming-integrations section (Sunshine/Apollo auto, process globs for
  Parsec/Chrome Remote Desktop, `-streaming_connect/-disconnect` for Steam);
  corrected stale component/parity tables.
- Model-specific settings without a stable public key (e.g. oledCareMode #350,
  screensaver clock #373) are set via the generic `-settings_other`/`-request`
  escape hatch rather than shipping guessed commands.

## [0.1.0] — 2026-09-12

First public release. A feature-parity Linux port of
[LGTV Companion](https://github.com/JPersson77/LGTVCompanion) for Windows,
designed to run on immutable/ostree distros (Bazzite) and regular desktops.

### Fixed
- **webOS 2025/2026 pairing** (upstream #351): switched the pairing handshake to
  the signature-free generic manifest. The old signed "LG Remote App" manifest
  is blacklisted by firmware 43.00.92+ (`403 blacklisted certificate detected`,
  or pairs but denies the button/pointer socket with `401`). The generic
  manifest pairs cleanly and is granted the full permission set via the on-screen
  prompt on new and old firmware. Verified on a webOS-2025 B4. A clear
  re-pair hint is raised on any `401 insufficient permissions`.

### Added (addressing upstream feature requests)
- **MQTT / Home Assistant bridge** (`lgtvc-mqtt`, optional `[mqtt]` extra): publishes
  per-TV power/screen/input/state/idle with Home Assistant MQTT auto-discovery and
  accepts commands — the community's most-requested automation path, which upstream
  has no first-party equivalent for. See `docs/mqtt.md`.
- **Session lock/unlock → TV** (#288): blank or power off the TV when the screen
  locks and restore on unlock (`on_lock`/`on_unlock`), via `org.freedesktop.ScreenSaver`.
- **Generic remote-stream detection** (#266 Parsec, #257 Chrome Remote Desktop,
  Apollo): configurable process-watch list in addition to the Sunshine log tail.

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
- Steam Remote Play has no dedicated auto-detector, but the generic
  `remote_stream.processes` watch covers it (and Parsec/CRD/etc.) when
  configured; Sunshine/Apollo are auto-detected.
- Still planned: a GUI-less user-mode `setup install --mode user` (system mode
  works today; user mode is ad-hoc via `--host`), and a version-check
  notification (the `update_check` option is currently inert).
