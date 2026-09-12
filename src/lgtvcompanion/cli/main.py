"""lgtvc: LGTVcli-compatible token-stream CLI.

Syntax (upstream-compatible):
    lgtvc -poweron
    lgtvc -backlight 80 -mute LivingRoom
    lgtvc -sethdmi 2 tv1 tv2
Commands are case-insensitive, several may appear per invocation, trailing
bare tokens select devices by id/name (none = all configured devices).

Extras: `lgtvc status` / `lgtvc reload` (daemon state / re-read config),
`lgtvc events` (print the SYSTEM_* stream), `lgtvc setup …`,
`--direct` (bypass the daemon and speak SSAP straight to the TVs),
`--host/--key` for ad-hoc use against an unconfigured TV.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any

from .. import config as config_mod
from .. import ipc
from ..ssap.commands import Command, Kind, lookup
from .output import format_result


@dataclass
class Invocation:
    commands: list[tuple[Command, list[Any]]]
    devices: list[str]
    output_mode: str = "default"
    output_key: str | None = None


class ParseError(Exception):
    pass


def parse_tokens(tokens: list[str]) -> Invocation:
    inv = Invocation(commands=[], devices=[])
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-") and not _is_negative_number(tok):
            name = tok.lstrip("-")
            if name.lower() in ("output", "od", "of", "ok"):
                i = _parse_output(inv, tokens, i)
                continue
            cmd = lookup(name)
            if cmd is None:
                raise ParseError(f"unknown command: {tok}")
            args: list[Any] = []
            j = i + 1
            for spec in cmd.args:
                if j >= len(tokens):
                    break
                nxt = tokens[j]
                if nxt.startswith("-") and not _is_negative_number(nxt) \
                        and lookup(nxt.lstrip("-")) is not None \
                        and len(args) >= cmd.min_args:
                    break
                try:
                    args.append(spec.parse(nxt))
                except ValueError as e:
                    raise ParseError(f"-{cmd.name}: {e}") from None
                j += 1
            if len(args) < cmd.min_args:
                needed = ", ".join(a.name for a in cmd.args[len(args):cmd.min_args])
                raise ParseError(f"-{cmd.name}: missing argument(s): {needed}")
            inv.commands.append((cmd, args))
            i = j
        else:
            inv.devices.append(tok)
            i += 1
    if not inv.commands:
        raise ParseError("no command given (try: lgtvc -help)")
    return inv


def _is_negative_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _looks_like_flag(tok: str) -> bool:
    return tok.startswith("-") and not _is_negative_number(tok)


def _take_key(tokens: list[str], idx: int, ctx: str) -> str:
    """Read a webOS key name at tokens[idx]. Keys are never dash-prefixed, so a
    flag-looking token means the key was omitted — a clear error rather than
    silently swallowing the next command/device."""
    if idx >= len(tokens) or _looks_like_flag(tokens[idx]):
        raise ParseError(f"{ctx}: missing key name")
    return tokens[idx]


def _parse_output(inv: Invocation, tokens: list[str], i: int) -> int:
    name = tokens[i].lstrip("-").lower()
    if name == "od":
        inv.output_mode = "default"
        return i + 1
    if name == "of":
        inv.output_mode = "friendly"
        return i + 1
    if name == "ok":
        inv.output_mode = "key"
        inv.output_key = _take_key(tokens, i + 1, "-ok")
        return i + 2
    # -output <mode> [key]
    if i + 1 >= len(tokens) or _looks_like_flag(tokens[i + 1]):
        raise ParseError("-output: missing mode (default|friendly|key)")
    mode = tokens[i + 1].lower()
    if mode not in ("default", "friendly", "key"):
        raise ParseError(f"-output: bad mode {mode!r}")
    inv.output_mode = mode
    if mode == "key":
        inv.output_key = _take_key(tokens, i + 2, "-output key")
        return i + 3
    return i + 2


def print_help() -> None:
    from ..ssap.commands import COMMANDS
    print(__doc__)
    print("Commands:")
    for name in sorted(COMMANDS):
        cmd = COMMANDS[name]
        argspec = " ".join(f"[{a.name}]" for a in cmd.args)
        print(f"  -{name} {argspec}".ljust(46) + cmd.help)


# -- execution paths ------------------------------------------------------------


async def run_via_daemon(inv: Invocation, socket_path: str) -> int:
    client = ipc.IpcClient(socket_path)
    await client.connect()
    status = 0
    try:
        for cmd, args in inv.commands:
            resp = await client.request(cmd.name, args, inv.devices)
            if not resp.get("ok"):
                print(f"error: {resp.get('error')}", file=sys.stderr)
                status = 1
                continue
            print(format_result(resp.get("results"), inv.output_mode, inv.output_key))
    finally:
        await client.close()
    return status


async def run_direct(inv: Invocation, cfg: config_mod.Config | None,
                     host: str | None, key: str | None) -> int:
    from ..ssap.handshake import KeyStore
    from ..daemon.devices import DeviceSession

    devices: list[config_mod.DeviceConfig] = []
    keystore: KeyStore
    if host:
        devices = [config_mod.DeviceConfig(id="adhoc", host=host)]
        keystore = KeyStore(config_mod.user_state_path() / "keys")
        if key:
            keystore.save("adhoc", key)
    elif cfg is not None:
        wanted = [d.lower() for d in inv.devices]
        devices = [d for d in cfg.devices
                   if not wanted or d.id.lower() in wanted or d.name.lower() in wanted]
        state = (config_mod.SYSTEM_STATE
                 if os.access(config_mod.SYSTEM_STATE, os.R_OK)
                 else config_mod.user_state_path())
        keystore = KeyStore(state / "keys")
    if not devices:
        print("error: no devices (configure some or pass --host)", file=sys.stderr)
        return 1

    status = 0
    sessions = []
    for dev in devices:
        session = DeviceSession(dev, keystore)
        session.client.on_pairing_prompt = lambda: print(
            "Approve the connection on the TV…", file=sys.stderr)
        sessions.append(session)
    try:
        # one output line per command (matches the daemon path), so repeated
        # commands (e.g. two -request) don't collide
        for cmd, args in inv.commands:
            if cmd.kind == Kind.META:
                print(f"error: -{cmd.name} needs the daemon", file=sys.stderr)
                status = 1
                continue
            results: dict[str, Any] = {}
            for session in sessions:
                try:
                    results[session.cfg.id] = await session.execute(cmd, args)
                except Exception as e:
                    results[session.cfg.id] = {"error": str(e)}
                    status = 1
            print(format_result(results, inv.output_mode, inv.output_key))
    finally:
        for session in sessions:
            await session.disconnect()
    return status


async def run_daemon_verb(verb: str, socket_path: str) -> int:
    """`lgtvc status` / `lgtvc reload` — daemon-only verbs, printed as JSON."""
    if not os.path.exists(socket_path):
        print("error: daemon not running", file=sys.stderr)
        return 1
    client = ipc.IpcClient(socket_path)
    await client.connect()
    try:
        resp = await client.request(verb, [], [])
    finally:
        await client.close()
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    print(format_result(resp.get("results"), "friendly"))
    return 0


async def run_events(socket_path: str) -> int:
    client = ipc.IpcClient(socket_path)
    await client.connect()
    try:
        async for event in client.subscribe():
            print(event, flush=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await client.close()
    return 0


def _socket_path(cli_value: str | None) -> str:
    return cli_value or ipc.default_socket()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] == "setup":
        from .setup_cmds import main as setup_main
        raise SystemExit(setup_main(argv[1:]))

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--direct", action="store_true")
    pre.add_argument("--host")
    pre.add_argument("--key")
    pre.add_argument("--socket")
    pre.add_argument("--config")
    pre.add_argument("--wait-network", action="store_true",
                     help="wait for the network before acting (resume path)")
    opts, rest = pre.parse_known_args(argv)

    if opts.wait_network:
        from ..daemon.network import wait_for_network
        cfg_probe = config_mod.Path(opts.config) if opts.config \
            else config_mod.find_config()
        host = opts.host
        if host is None and cfg_probe and config_mod.Path(cfg_probe).exists():
            devs = config_mod.load(config_mod.Path(cfg_probe)).devices
            host = devs[0].host if devs else None
        if host:
            asyncio.run(wait_for_network(host))

    if rest and rest[0] == "events":
        raise SystemExit(asyncio.run(run_events(_socket_path(opts.socket))))

    if rest and rest[0] in ("status", "reload"):
        raise SystemExit(asyncio.run(
            run_daemon_verb(rest[0], _socket_path(opts.socket))))

    if not rest or rest[0] in ("-help", "--help", "-h"):
        print_help()
        raise SystemExit(0)

    try:
        inv = parse_tokens(rest)
    except ParseError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    cfg = None
    cfg_path = config_mod.Path(opts.config) if opts.config else config_mod.find_config()
    if cfg_path and config_mod.Path(cfg_path).exists():
        cfg = config_mod.load(config_mod.Path(cfg_path))

    socket_path = _socket_path(opts.socket)
    use_daemon = (not opts.direct and not opts.host
                  and os.path.exists(socket_path))
    if use_daemon:
        raise SystemExit(asyncio.run(run_via_daemon(inv, socket_path)))
    raise SystemExit(asyncio.run(run_direct(inv, cfg, opts.host, opts.key)))


if __name__ == "__main__":
    main()
