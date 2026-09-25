# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

_Nothing yet._

## [0.2.7] — 2026-09-25

### Fixed
- **Steam Controller detection now survives suspend/resume.** Three hotplug
  bugs compounded: the hidraw monitor's inotify watch pointed at `/dev/hidraw`
  (not a directory — hidraw nodes live directly in `/dev`), so it silently fell
  back to a 45 s rescan; an EOF read from a re-enumerated device never evicted
  the dead fd; and a node re-created under the *same* `/dev/hidrawN` name was
  never reopened (the scan deduped by path). After a sleep/wake cycle the
  controller's nodes are re-enumerated and the agent kept reading dead fds until
  restart. The monitor now watches `/dev` with hidraw-name filtering, evicts on
  EOF, revalidates open fds against the node's inode on every scan, and always
  runs the periodic rescan as a backstop — a replaced node gets a fresh detector
  (fresh warmup + runaway guard). The evdev monitor gains the same hardening.
  On top of that, a resume is detected directly (the CLOCK_BOOTTIME −
  CLOCK_MONOTONIC delta grows by exactly the suspended time — no logind needed)
  and all controller detectors are reset: even when the nodes survive suspend
  untouched, the stale volatility masks and the permanently-latching
  "unparseable" guard no longer outlive a sleep/wake cycle.
- **Wake-on-input now works on QuickStart+ TVs.** The wake gate was a bare TCP
  probe of the API port — but TVs with QuickStart+ (e.g. 2025 OLEDs) keep port
  3001 accepting in Active Standby, so a remote-control power-off read as "TV is
  on" and key presses never woke it. The daemon now asks each TV for its real
  power state (`getPowerState` over a short-lived probe session, 3 s budget) and
  powers on only TVs that aren't `Active` — so it still never touches a TV that
  is showing another input or app. A rejected pairing key suppresses waking
  (each register would pop an on-screen pairing prompt) and logs a re-pair hint.
  Note: with `persistent_connection: "off"`, a key press while the TV is on now
  opens a brief probe session at most once per cooldown window.

### Changed
- **Steam Controller input can now power on a fully-off TV** — new
  `steam_controller.wake` knob, **on by default** (matching keyboard
  wake-on-input). The hidraw detector can't tell buttons from stick movement, so
  any genuine controller input wakes an off TV; restore the old unblank-only
  behavior with `lgtvc setup steam-controller --wake off` (or the tray
  checkbox).

## [0.2.6] — 2026-09-15

### Added
- **Steam Controller keeps the TV awake in gamescope / Game Mode.** In a
  gamescope session the Steam client claims the controller over hidraw and the
  kernel stops emitting its evdev events, so controller-only use let the TV blank
  mid-session (a keyboard, never claimed by Steam, still worked). The agent now
  reads the controller's `/dev/hidraw*` node directly — read-only, so Steam is
  undisturbed — with an offset-agnostic detector that ignores the idle carrier
  stream and fails safe on anything it can't parse. On by default (`steam_controller`,
  inert without a Valve `28de` controller); toggle with `lgtvc setup
  steam-controller on|off` or the tray checkbox. Diagnose access/input with
  `lgtvc setup hidraw-scan [--seconds N]`. Controller input unblanks the TV but
  won't power on a fully-off one.

## [0.2.5] — 2026-09-14

### Fixed
- **Controller input now keeps the screen awake.** The idle monitor's analog-stick
  jitter deadband was also swallowing D-pad/hat presses (discrete -1/0/1) and
  trigger pulls (0..255) — so navigating with a gamepad (e.g. a Steam Controller)
  registered no activity and the TV would blank mid-use. D-pads and triggers now
  bypass the deadband; real sticks keep it.
- **Faster, more reliable PC-presence for Home Assistant** (MQTT bridge): on a
  clean shutdown the bridge now publishes `offline` itself (the retained Will
  only fires on an *ungraceful* drop), and the keepalive dropped from 60s to 30s
  so a hard power-off is detected in ~45s instead of ~90s.

## [0.2.4] — 2026-09-14

### Added
- **Offline mode** (`offline_mode`, off by default): a privacy master-switch that
  guarantees the app makes zero internet connections — disables the daily update
  check and refuses a non-LAN MQTT broker (a LAN Home Assistant broker still
  works). TV control was already LAN-only. Toggle with
  `lgtvc setup offline-mode on|off` or the tray Settings checkbox.

### Fixed
- Chained CLI commands now **stop on a dispatch failure** instead of continuing:
  if a command can't run (unknown command/device, daemon error) the chain halts
  with exit 1, so `lgtvc -sethdmi 2 -mute` can't land half-applied. Per-device
  TV errors keep going (one TV failing shouldn't abort a multi-TV chain) but now
  also set a non-zero exit so scripts can detect them.

## [0.2.3] — 2026-09-14

### Added
- **SLSA Build Level 3 provenance** on every release: a Sigstore-signed
  `multiple.intoto.jsonl` (generated by the audited `slsa-github-generator`
  reusable workflow) covering the wheel, sdist, `.deb`, `.rpm`, and Arch
  package. Verify with `slsa-verifier` — see the README. PyPI keeps its PEP 740
  attestations.

## [0.2.2] — 2026-09-14

### Added
- **Native packages** on every release: `.deb`, `.rpm`, and Arch, built with
  nfpm. Pure-Python noarch payload under `/opt/lgtv-companion` + all systemd
  units; only needs `python3 ≥ 3.11`. See [packaging/README.md](packaging/README.md).
  The **MQTT/Home Assistant bridge is bundled**. The **tray GUI** is included
  and pulls PySide6 automatically on Fedora/Arch (a one-line `pip --target`
  install on Debian/Ubuntu, which don't package PySide6); it works on any
  StatusNotifierItem desktop (KDE/XFCE/MATE/Cinnamon/…; GNOME needs an
  AppIndicator extension). Each release's packages are install-smoke-tested in
  Ubuntu/Fedora/Arch containers.

### Changed
- Tray icon: prefer a bundled hicolor icon so it renders on any desktop/theme.

## [0.2.1] — 2026-09-14

Maintenance & quality release. No behavior changes — existing configs and
commands work exactly as in 0.2.0.

### Added
- **Ships type hints** (`py.typed`, PEP 561): downstream projects and editors
  now see the package's inline types.
- Project metadata: a Funding link and a refreshed, Bazzite-first README with
  live CI / coverage / PyPI badges.

### Changed
- Internal: the tray app's menu-building was refactored into testable helpers
  (`build_menu`), and a dead helper was removed from `setup`. No user-visible
  difference.

### Quality (not user-facing, for the curious)
- Test coverage raised from ~53% to **99%** (486 tests), including the Qt tray
  UI (offscreen) and real D-Bus signal binding, run in CI.
- CI hardening: uv-based matrix (Python 3.11–3.13), auto-updating badges,
  Dependabot, and a Trusted-Publishing release workflow.

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
