# External API (scripting)

The daemon exposes a small line-based (NDJSON) protocol on a Unix socket —
`/run/lgtv-companion/ipc.sock` in system mode (`$XDG_RUNTIME_DIR/lgtv-companion/ipc.sock`
in user mode) — for scripting. This is the Linux replacement for the Windows
named-pipe API, using the same `SYSTEM_*` event vocabulary.

## Remote-streaming integrations

When you stream your desktop to another device you usually want the local TV
off/blanked. Three ways to drive that:

- **Sunshine / Apollo** — auto-detected: the agent tails `sunshine.log` for
  `CLIENT CONNECTED/DISCONNECTED`. Just set `remote_stream.enabled: true`.
- **Other clients by process** (Parsec, Chrome Remote Desktop, Moonlight host, …)
  — add name globs to `remote_stream.processes`, e.g.
  `["parsec*", "chrome-remote-desktop*", "moonlight*"]`. The agent matches both
  the short `comm` and the full `argv[0]` basename.
- **Steam Remote Play / anything scriptable** — Steam's host process isn't
  reliably distinguishable from an idle Steam, so there's no auto-detector.
  Instead call the hooks from a wrapper or launch options:
  ```sh
  lgtvc -streaming_connect      # blank/off the TV per remote_stream.on_connect
  # … your streaming session …
  lgtvc -streaming_disconnect   # restore per remote_stream.on_disconnect
  ```
  (`on_disconnect` = `on` | `keep_off` | `restore`.)

## Subscribe to events

```sh
lgtvc events
```

Prints one event per line as they happen. Event names:

| Event | When |
|---|---|
| `SYSTEM_USER_IDLE` / `SYSTEM_USER_BUSY` | user-idle mode blanks / unblanks |
| `SYSTEM_DISPLAYS_OFF` / `SYSTEM_DISPLAYS_ON` | (reserved) |

> Power-transition events (`SYSTEM_SUSPEND`/`RESUME`/`SHUTDOWN`/`REBOOT`) are
> **not** emitted by the daemon in this version: suspend/resume and shutdown are
> handled by the `lgtvc-sleep` / `lgtvc-shutdown` oneshot units, not the daemon.
> Hook those transitions with your own systemd units ordered around
> `sleep.target` / `poweroff.target` if you need them.

Example — flip an HDMI input on your AVR whenever the TV blanks:

```sh
lgtvc events | while read -r ev; do
  case "$ev" in
    SYSTEM_USER_IDLE) my-avr-standby ;;
    SYSTEM_USER_BUSY) my-avr-on ;;
  esac
done
```

## Send commands

Any `lgtvc` command reaches the daemon over the same socket, so scripts just
call the CLI:

```sh
lgtvc -poweron LivingRoom
lgtvc -backlight 40
lgtvc -output key -ok state -request com.webos.service.tvpower/power/getPowerState
lgtvc status          # daemon + device state as JSON
lgtvc reload          # re-read the config file
```

## Raw protocol

If you'd rather talk to the socket directly, each request is one JSON line:

```json
{"id": 1, "cmd": "backlight", "args": [40], "devices": ["tv1"]}
```

and the response:

```json
{"id": 1, "ok": true, "results": {"tv1": {"returnValue": true}}}
```

Send `{"subscribe": true}` to receive `{"event": "SYSTEM_…"}` lines on that
connection.
