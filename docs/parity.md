# Feature parity with upstream (Windows LGTV Companion)

| Upstream feature | This port |
|---|---|
| Power sync: boot / shutdown / **reboot** / suspend / resume | ✅ shutdown/reboot via `lgtvc-shutdown` (`Conflicts=reboot.target` — deterministic, no localized word lists), suspend/resume via `lgtvc-sleep` (both `sleep.target` oneshots), boot via the daemon |
| Wake-on-LAN (broadcast / subnet / directed / auto, multi-MAC) | ✅ |
| HDMI source-safety guard on power-off (only if TV on the PC's input) | ✅ per-device `getForegroundAppInfo` check |
| Set HDMI input on power-on (with delay) | ✅ |
| User-idle screen blank (+ mute), ignored keys, process control list | ✅ evdev instead of Raw Input; mouse debounce, stick deadband, accelerometer exclusion |
| Fullscreen / "video wake lock" idle vetoes | MPRIS playback + logind idle inhibitors + KWin fullscreen (KDE, best-effort) |
| Full settings CLI (picture / sound / inputs / ambient / service-menu / Flex) | ✅ 74 luna settings vendored verbatim from upstream `lg_api_commands.h`, identical names/ranges/enums |
| Virtual remote buttons | ✅ pointer-input socket, 76 buttons from upstream `lg_api_buttons.h` |
| Multiple TVs | ✅ |
| Display-topology mode (power TVs by attached displays) | ✅ DRM connector + EDID (`lgtvc setup map-display`) |
| Remote-stream detection | ✅ Sunshine (log tail via the agent) + explicit `-streaming_connect/_disconnect` hooks; Steam Remote Play not auto-detected |
| External scripting API | ✅ Unix socket, same `SYSTEM_*` event names (was a named pipe) |
| Settings GUI + tray | ✅ PySide6 tray + settings dialog (`[tray]` extra) |
| Auto-updater (MSI) | N/A — use `pip`/`pipx`; a version-check notification is planned |
| RDP session detection | N/A on Linux |
| NIC LUID source binding | replaced by an `interface` name (optional) |
| Localized restart/shutdown word lists | N/A — systemd tells us reboot vs shutdown deterministically |

## Intentional differences

- **Architecture.** Upstream runs one service + a per-user daemon. Here the
  long-running system daemon owns TV sessions, boot power-on, wake-on-input,
  idle and IPC; power *transitions* are short-lived systemd oneshot units (the
  proven pattern that lets systemd natively order the TV-off before network
  teardown, with no in-process inhibitor). A per-user **agent** supplies
  session-only facts (input activity, MPRIS, KWin fullscreen) the confined
  system service can't see.
- **Reboot vs shutdown is deterministic** (systemd), an improvement over
  matching localized event-log strings.
- **Power-transition `SYSTEM_*` events** are not emitted on the IPC bus in this
  version (the oneshot units own those transitions); idle events still are.
