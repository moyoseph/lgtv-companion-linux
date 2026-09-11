# Troubleshooting

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
