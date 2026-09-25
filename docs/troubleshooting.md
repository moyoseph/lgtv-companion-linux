# Troubleshooting

## Pairing fails on webOS 2025/2026 (`403 blacklisted certificate detected`), or buttons stop working (`401 insufficient permissions`)

webOS firmware 43.00.92+ blacklisted the long-published "LG Remote App" signed
manifest. Apps that still send it either fail to pair (`403 blacklisted
certificate detected`) or pair but are denied elevated permissions — the
pointer-input/button socket then fails with `401 insufficient permissions`
(upstream issue #351).

This port uses the modern **signature-free generic manifest** (see
`ssap/handshake.py`), which pairs cleanly and is granted the full permission
set via the on-screen prompt on webOS 2025/2026 and older firmware alike — so
you shouldn't hit this. If you paired with an older build (or another app's
blacklisted key) and see `401 insufficient permissions`, just re-pair:

```sh
sudo lgtvc setup pair --host <tv-ip>     # approve the prompt on the TV
```

An existing, already-trusted key keeps working after the manifest change (no
re-pair needed) — re-pairing is only required if a command reports 401.

## The daemon can't reach the TV: `unreachable … [Errno 13] Permission denied`

If `lgtvc -poweron` (via the daemon) returns EACCES on every attempt, but
`lgtvc --host <ip> -poweron` or an interactive run works, the daemon unit is
almost certainly launching via the **pip console-script entry point** instead
of `python -m`.

On ostree + SELinux (e.g. Bazzite), exec'ing the `lib_t`-labelled
`/usr/local/lib/lgtv-companion/bin/lgtvc-daemon` wrapper leaves the service in a
process domain where all outbound `connect()` calls fail with EACCES. Launching
the interpreter directly is unaffected.

Check:

```sh
grep ExecStart /etc/systemd/system/lgtvc-daemon.service
```

It must be:

```
ExecStart=/usr/local/lib/lgtv-companion/bin/python3 -m lgtvcompanion.daemon.main --config /etc/lgtv-companion/config.json
```

Re-run `sudo lgtvc setup install --mode system` to regenerate correct units.
This is not SELinux to be "fixed" with a policy — `python -m` simply avoids the
problematic exec.

## The daemon can't read `/dev/input`

Expected. As a confined system service it can't open the seat-owned input
devices, so wake-on-input and idle detection are driven by the **user agent**
(`lgtvc-agent`), which reads `/dev/input` via the login seat's uaccess ACL and
reports over IPC. Make sure the agent is running:

```sh
systemctl --user status lgtvc-agent
```

## The TV blanks while I'm using a Steam Controller in Game Mode (gamescope)

A keyboard keeps the TV awake but the Steam Controller doesn't. This is expected
of plain evdev monitoring: in a gamescope session the Steam client claims the
controller over `/dev/hidraw*`, and the kernel `hid-steam` driver then stops
emitting the controller's evdev events (a keyboard is never claimed, so it still
works). The agent works around this by reading the controller's hidraw node
**directly** — it's on by default and inert on any system without a Valve
(`28de`) controller.

If it isn't working:

```sh
lgtvc setup hidraw-scan --seconds 3   # is the controller readable? do bytes move?
lgtvc setup steam-controller          # prints on|off (default: on)
systemctl --user restart lgtvc-agent  # the agent reads the setting at startup
```

`hidraw-scan` should list your controller as `… 28de:xxxx  readable … <- Valve`
and, with `--seconds`, show its report changing while you press buttons. If it
shows `NO READ ACCESS`, install Steam's udev rules (the `steam-devices` package —
present by default on Bazzite/Steam Deck) so the seat user can read the node. The
detector only ever *reads* the controller, so Steam is undisturbed.

By default controller input can also **power on a fully-off TV**
(`steam_controller.wake`). The detector can't tell buttons from stick movement,
so *any* genuine controller input — including picking the controller up — wakes
an off TV. If you'd rather the controller only unblank:

```sh
lgtvc setup steam-controller --wake off
systemctl --user restart lgtvc-agent
```

## Key presses don't wake the TV from standby (QuickStart+)

Wake-on-input asks the TV for its **real power state** rather than just probing
the API port: TVs with QuickStart+ (e.g. 2025 OLEDs) keep port 3001 accepting
connections in Active Standby, so "the port is open" cannot distinguish *on*
from *standby* (versions before 0.2.7 got this wrong and never woke such TVs).
A TV that reports `Active` — whatever app or input it is showing — is never
touched, so wake-on-input still can't yank a TV somebody is watching.

If key presses still don't wake the TV, check the daemon log:

```sh
journalctl -u lgtvc-daemon -b | grep -i wake
```

A `stored client key rejected` warning means the TV no longer accepts your
pairing key — re-pair with `sudo lgtvc setup pair --host <tv-ip>`. (Waking is
deliberately suppressed on a rejected key, otherwise every probe would pop a
pairing prompt on-screen.)

## Nothing happens on suspend / resume

Suspend/resume are handled by the `lgtvc-sleep` oneshot unit (bound to
`sleep.target`), not the daemon. Verify it's enabled and check its log:

```sh
systemctl is-enabled lgtvc-sleep
journalctl -u lgtvc-sleep -b
```

Note: `rtcwake -m mem` is a raw kernel suspend that **bypasses**
logind/`sleep.target`, so it won't trigger the unit. To test the real path use
`rtcwake -m no -s 90 && systemctl suspend`.

## The TV stays on when I shut down / powers off on reboot

Reboot-vs-shutdown is deterministic via the `lgtvc-shutdown` unit
(`Conflicts=reboot.target`, `WantedBy=poweroff.target halt.target`): a reboot
leaves the TV on, a shutdown powers it off. If it's wrong, confirm the unit is
enabled: `systemctl is-enabled lgtvc-shutdown`.

## Wake-on-LAN doesn't wake the TV

- Ensure the TV's "Turn on via network / Wi-Fi" setting is on (`lgtvc setup
  pair` enables it automatically after pairing; or `lgtvc -wol on`).
- A WiFi TV in deep standby may need the directed-broadcast method: set
  `wol_method: "subnet"` (default) or `"auto"` for the device.
- Confirm the MAC in the config matches the TV's active interface (WiFi vs
  wired have different MACs).

## A TV command works via `--direct` but not via the daemon

The daemon shares one live session per device and serializes/coalesces
requests. If `--direct` works but the daemon path errors, check
`journalctl -u lgtvc-daemon` — usually a stale pairing key (`lgtvc setup pair`)
or the daemon isn't running.
