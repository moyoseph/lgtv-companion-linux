"""lgtvc setup: install units, pair, import/migrate/rollback legacy config."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config as config_mod
from ..ssap.handshake import KeyStore

DAEMON_UNIT = "lgtvc-daemon.service"
SHUTDOWN_UNIT = "lgtvc-shutdown.service"
SLEEP_UNIT = "lgtvc-sleep.service"
MQTT_UNIT = "lgtvc-mqtt.service"
SYSTEM_UNIT_DIR = Path("/etc/systemd/system")

# Optional MQTT/Home Assistant bridge. System service (needs the /run IPC
# socket + broker creds, must run without a login). python -m, like the daemon.
MQTT_UNIT_TEMPLATE = """\
[Unit]
Description=LGTV Companion MQTT / Home Assistant bridge
After=lgtvc-daemon.service network-online.target
Wants=network-online.target
BindsTo=lgtvc-daemon.service

[Service]
Type=simple
{user_line}ExecStart={python_bin} -m lgtvcompanion.mqtt.main
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

DAEMON_UNIT_TEMPLATE = """\
[Unit]
Description=LGTV Companion daemon
Wants=network-online.target
After=network-online.target NetworkManager-wait-online.service

[Service]
Type=simple
# Launch the venv interpreter directly with -m, NOT the pip console-script
# entry point: on Bazzite (ostree + SELinux), exec'ing the lib_t-labelled
# lgtvc-daemon wrapper leaves the process in a domain whose outbound socket
# connects all fail with EACCES. `python -m` is unaffected. (Cost a very long
# debugging session to isolate — do not "simplify" back to the console script.)
{user_line}ExecStart={python_bin} -m lgtvcompanion.daemon.main --config {config_path}
Restart=on-failure
RestartSec=3
RuntimeDirectory=lgtv-companion
StateDirectory=lgtv-companion
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

# Reboot leaves the TV on; only poweroff/halt pull this in (Conflicts=reboot
# gives deterministic reboot-vs-shutdown). systemd waits for this oneshot
# before proceeding, so no daemon inhibitor is needed.
SHUTDOWN_UNIT_TEMPLATE = """\
[Unit]
Description=Power off LG TV on shutdown (not reboot)
Conflicts=reboot.target
DefaultDependencies=no
Before=poweroff.target halt.target

[Service]
Type=oneshot
ExecStart={python_bin} -m lgtvcompanion.cli.main --direct --config {config_path} -poweroff
TimeoutStartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=poweroff.target halt.target
"""

# Suspend/resume via sleep.target ordering (the proven legacy pattern):
# ExecStart (off) runs and systemd WAITS for it before suspending; ExecStop
# (on) runs at resume. Short-lived processes, so no inhibitor/EACCES issue.
SLEEP_UNIT_TEMPLATE = """\
[Unit]
Description=Power off LG TV on suspend, on at resume
Before=sleep.target
StopWhenUnneeded=yes

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={python_bin} -m lgtvcompanion.cli.main --direct --config {config_path} -poweroff
ExecStop={python_bin} -m lgtvcompanion.cli.main --direct --config {config_path} --wait-network -poweron
TimeoutStartSec=15
TimeoutStopSec=45
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=sleep.target
"""

AGENT_UNIT = "lgtvc-agent.service"
USER_UNIT_DIR = Path("/etc/systemd/user")

# Runs in the graphical session: /dev/input is readable there via the seat's
# uaccess ACL, which the SELinux-confined system daemon is denied.
AGENT_UNIT_TEMPLATE = """\
[Unit]
Description=LGTV Companion session agent
PartOf=graphical-session.target
After=graphical-session.target

[Service]
# python -m, not the console script — same reason as the daemon unit
ExecStart={python_bin} -m lgtvcompanion.agent.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
"""

TRAY_UNIT = "lgtvc-tray.service"

# Self-skips outside a real desktop: gamescope/Steam Game Mode DOES register a
# StatusNotifierWatcher but has no usable Qt tray (QSystemTrayIcon SIGABRTs), so
# skip gamescope/SteamOS sessions explicitly, then require a tray host.
TRAY_UNIT_TEMPLATE = """\
[Unit]
Description=LGTV Companion tray
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecCondition=/bin/sh -c 'case "$XDG_CURRENT_DESKTOP" in *gamescope*|*steamos*|*Steam*) exit 1;; esac; busctl --user list --no-legend | grep -q StatusNotifierWatcher'
ExecStart={python_bin} -m lgtvcompanion.tray.app
Restart=on-failure
RestartSec=30

[Install]
WantedBy=graphical-session.target
"""

LEGACY_UNITS = ["lgtv-startup.service", "lgtv-shutdown.service", "lgtv-sleep.service"]
LEGACY_USER_UNIT = "tv-wake-on-input.service"


def _bin(name: str) -> str:
    sibling = Path(sys.argv[0]).resolve().parent / name
    if sibling.exists():
        return str(sibling)
    found = shutil.which(name)
    if found:
        return found
    sys.exit(f"cannot locate {name} — is the package installed?")


def _python_bin() -> str:
    """The venv interpreter. Units exec this with -m rather than the console
    scripts (see the daemon unit comment for why)."""
    return sys.executable


def _module_available(python_bin: str, module: str) -> bool:
    try:
        subprocess.run([python_bin, "-c", f"import {module}"],
                       check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def _tray_available(python_bin: str) -> bool:
    return _module_available(python_bin, "PySide6")


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=False)


def cmd_install(args: argparse.Namespace) -> int:
    if args.mode != "system":
        sys.exit("only --mode system is implemented in v0.1")
    if os.geteuid() != 0:
        sys.exit("system install needs root (sudo lgtvc setup install --mode system)")
    config_path = config_mod.SYSTEM_CONFIG
    config_path.parent.mkdir(parents=True, exist_ok=True)
    user_line = f"User={args.service_user}\n" if args.service_user != "root" else ""
    python_bin = _python_bin()
    (SYSTEM_UNIT_DIR / DAEMON_UNIT).write_text(DAEMON_UNIT_TEMPLATE.format(
        python_bin=python_bin, config_path=config_path, user_line=user_line))
    (SYSTEM_UNIT_DIR / SHUTDOWN_UNIT).write_text(SHUTDOWN_UNIT_TEMPLATE.format(
        python_bin=python_bin, config_path=config_path))
    (SYSTEM_UNIT_DIR / SLEEP_UNIT).write_text(SLEEP_UNIT_TEMPLATE.format(
        python_bin=python_bin, config_path=config_path))
    mqtt_available = _module_available(python_bin, "aiomqtt")
    if mqtt_available:
        (SYSTEM_UNIT_DIR / MQTT_UNIT).write_text(MQTT_UNIT_TEMPLATE.format(
            python_bin=python_bin, user_line=user_line))
    USER_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (USER_UNIT_DIR / AGENT_UNIT).write_text(AGENT_UNIT_TEMPLATE.format(
        python_bin=python_bin))
    # tray unit only if PySide6 is importable (the [tray] extra is installed)
    tray_available = _tray_available(python_bin)
    if tray_available:
        (USER_UNIT_DIR / TRAY_UNIT).write_text(TRAY_UNIT_TEMPLATE.format(
            python_bin=python_bin))
    _run(["systemctl", "--global", "enable", AGENT_UNIT], check=False)
    _run(["systemctl", "daemon-reload"])
    print(f"installed {DAEMON_UNIT}, {SHUTDOWN_UNIT} (not enabled) "
          f"and {AGENT_UNIT} (user, globally enabled)")
    if tray_available:
        print(f"tray unit installed — enable per user with: "
              f"systemctl --user enable --now {TRAY_UNIT}")
    else:
        print("tray not installed (PySide6 missing) — "
              "pip install 'lgtvcompanion[tray]' then re-run setup install")
    if mqtt_available:
        print(f"MQTT bridge unit installed — set mqtt.enabled in the config, then: "
              f"sudo systemctl enable --now {MQTT_UNIT}")
    print("next: lgtvc setup import-legacy   (or: lgtvc setup pair --host <tv-ip>)")
    return 0


def cmd_import_legacy(args: argparse.Namespace) -> int:
    legacy = Path(args.legacy_dir)
    if not legacy.exists():
        sys.exit(f"{legacy} not found")
    cfg, client_key = config_mod.import_legacy(legacy)
    cfg.global_.dry_run = True  # soak first; migrate-legacy flips it off
    config_mod.save(cfg, config_mod.SYSTEM_CONFIG)
    print(f"wrote {config_mod.SYSTEM_CONFIG} (dry_run=true for the soak)")
    if client_key:
        store = KeyStore(config_mod.SYSTEM_STATE / "keys")
        store.save(cfg.devices[0].id, client_key)
        print(f"client key copied to {store.path(cfg.devices[0].id)}")
    else:
        print("no legacy client.key found — run: lgtvc setup pair")
    return 0


def cmd_import_windows(args: argparse.Namespace) -> int:
    src = Path(args.file)
    if not src.exists():
        sys.exit(f"{src} not found")
    cfg, keys = config_mod.import_windows(src)
    target = (config_mod.SYSTEM_CONFIG if os.geteuid() == 0
              else config_mod.user_config_path())
    config_mod.save(cfg, target)
    print(f"wrote {target} ({len(cfg.devices)} device(s))")
    state = (config_mod.SYSTEM_STATE if os.geteuid() == 0
             else config_mod.user_state_path())
    store = KeyStore(state / "keys")
    for device_id, key in keys.items():
        store.save(device_id, key)
        print(f"session key imported for {device_id}")
    if not keys:
        print("no session keys in the file — run: lgtvc setup pair")
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    cfg_path = config_mod.find_config()
    if args.host:
        device = config_mod.DeviceConfig(id=args.device, host=args.host)
    elif cfg_path:
        cfg = config_mod.load(cfg_path)
        found = cfg.device(args.device) or (cfg.devices[0] if cfg.devices else None)
        if found is None:
            sys.exit("no devices configured — pass --host")
        device = found
    else:
        sys.exit("no config found — pass --host <tv-ip>")

    async def do_pair() -> None:
        from ..ssap.client import SsapClient
        store = KeyStore((config_mod.SYSTEM_STATE if os.geteuid() == 0
                          else config_mod.user_state_path()) / "keys")
        client = SsapClient(
            device.host, use_ssl=device.ssl, client_key=None,
            on_new_key=lambda key: store.save(device.id, key),
            on_pairing_prompt=lambda: print(
                "→ Approve the connection on the TV (press OK on the prompt)…"))
        await client.connect(pairing=True)
        print(f"paired; key stored for device {device.id!r}")
        try:
            from ..ssap.luna import enable_tv_wol
            await enable_tv_wol(client)
            print("enabled the TV's 'Turn on via network' (WoL) setting")
        finally:
            await client.close()

    asyncio.run(do_pair())
    return 0


def cmd_migrate_legacy(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        sys.exit("needs root")
    cfg = config_mod.load(config_mod.SYSTEM_CONFIG)
    if cfg.global_.dry_run and not args.keep_dry_run:
        cfg.global_.dry_run = False
        config_mod.save(cfg, config_mod.SYSTEM_CONFIG)
        print("dry_run -> false")
    for unit in LEGACY_UNITS:
        _run(["systemctl", "disable", "--now", unit], check=False)
    if args.legacy_user:
        _run(["systemctl", "--machine", f"{args.legacy_user}@.host", "--user",
              "disable", "--now", LEGACY_USER_UNIT], check=False)
    _run(["systemctl", "enable", DAEMON_UNIT, SHUTDOWN_UNIT, SLEEP_UNIT])
    # restart (not just enable --now): a running daemon must reload the
    # config now that dry_run flipped off
    _run(["systemctl", "restart", DAEMON_UNIT])
    print("migrated: legacy units disabled, lgtvc units enabled")
    print("rollback anytime: sudo lgtvc setup rollback-legacy")
    return 0


def cmd_rollback_legacy(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        sys.exit("needs root")
    _run(["systemctl", "disable", "--now", DAEMON_UNIT, SHUTDOWN_UNIT, SLEEP_UNIT],
         check=False)
    for unit in LEGACY_UNITS:
        _run(["systemctl", "enable", "--now", unit], check=False)
    if args.legacy_user:
        _run(["systemctl", "--machine", f"{args.legacy_user}@.host", "--user",
              "enable", "--now", LEGACY_USER_UNIT], check=False)
    print("rolled back to the legacy lgtvcontrol units")
    return 0


def cmd_map_display(args: argparse.Namespace) -> int:
    from ..daemon.topology import connected_displays
    displays = connected_displays()
    if not displays:
        print("no connected displays with EDID found under /sys/class/drm")
        return 1
    print("connected displays:")
    for connector, key in displays.items():
        marker = "  <- LG" if key.startswith("GSM") else ""
        print(f"  {connector}: {key}{marker}")
    if not args.device:
        print("\nassign one with: lgtvc setup map-display --device tv1 [--key <key>]")
        return 0
    key = args.key
    if key is None:
        lg = [k for k in displays.values() if k.startswith("GSM")]
        if len(lg) != 1:
            print(f"{'no' if not lg else 'several'} LG display(s) found — "
                  "pass --key explicitly")
            return 1
        key = lg[0]
    cfg_path = config_mod.find_config()
    if cfg_path is None:
        sys.exit("no config found")
    cfg = config_mod.load(cfg_path)
    dev = cfg.device(args.device)
    if dev is None:
        sys.exit(f"unknown device {args.device!r}")
    dev.unique_display_key = key
    config_mod.save(cfg, cfg_path)
    print(f"{args.device}: unique_display_key = {key}")
    print("enable the feature with global.topology.enabled = true, then "
          "restart lgtvc-daemon")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = config_mod.find_config()
    if path is None:
        print("no config found")
        return 1
    print(f"# {path}")
    print(json.dumps(json.loads(path.read_text()), indent=2))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="lgtvc setup")
    sub = parser.add_subparsers(dest="action", required=True)

    p = sub.add_parser("install", help="install systemd units")
    p.add_argument("--mode", choices=["system", "user"], default="system")
    p.add_argument("--service-user", default=os.environ.get("SUDO_USER", "root"))
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("import-legacy", help="import /etc/lgtvcontrol settings")
    p.add_argument("--legacy-dir", default=str(config_mod.LEGACY_DIR))
    p.set_defaults(func=cmd_import_legacy)

    p = sub.add_parser("import-windows",
                       help="import an upstream LGTV Companion config.json")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_import_windows)

    p = sub.add_parser("pair", help="pair with a TV (shows a prompt on the TV)")
    p.add_argument("--device", default="tv1")
    p.add_argument("--host", help="TV IP (defaults to the configured device)")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser("migrate-legacy", help="cut over from the legacy units")
    p.add_argument("--legacy-user", default=os.environ.get("SUDO_USER") or None,
                   help="user owning the legacy tv-wake-on-input unit")
    p.add_argument("--keep-dry-run", action="store_true")
    p.set_defaults(func=cmd_migrate_legacy)

    p = sub.add_parser("rollback-legacy", help="switch back to the legacy units")
    p.add_argument("--legacy-user", default=os.environ.get("SUDO_USER") or None)
    p.set_defaults(func=cmd_rollback_legacy)

    p = sub.add_parser("map-display",
                       help="list connected displays / bind one to a device")
    p.add_argument("--device")
    p.add_argument("--key")
    p.set_defaults(func=cmd_map_display)

    p = sub.add_parser("show", help="print the active config")
    p.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)
