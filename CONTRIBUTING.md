# Contributing

Thanks for your interest! Bug reports, hardware confirmations (which TV models
work), and pull requests are all welcome.

## Dev setup

The project uses [uv](https://docs.astral.sh/uv/), but plain `venv` + `pip`
works too.

```sh
git clone https://github.com/moyoseph/lgtv-companion-linux
cd lgtv-companion-linux

# with uv
uv venv && uv pip install -e '.[dev]'

# or with stock tooling
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
```

## Checks (what CI runs)

```sh
ruff check src tests     # lint
mypy src                 # types (advisory)
pytest -q                # tests — no TV needed, uses a fake webOS server
```

The test suite talks to an in-process fake webOS TV, so you can develop and run
everything without real hardware.

## Layout

- `src/lgtvcompanion/ssap/` — the webOS SSAP websocket client, pairing, WoL
- `src/lgtvcompanion/daemon/` — the system service (sessions, idle, topology, IPC)
- `src/lgtvcompanion/agent/` — the session-side watcher (input, MPRIS, lock, streams)
- `src/lgtvcompanion/cli/` — the `lgtvc` command and `setup` subcommands
- `src/lgtvcompanion/data/` — vendored luna-settings and button tables
- `tests/` — pytest suite + fake TV server

## Guidelines

- Keep the CLI syntax compatible with the upstream Windows `LGTVcli`.
- Config is additive-tolerant: add new keys with sane defaults; don't break
  existing configs.
- systemd units invoke `python -m lgtvcompanion.…`, **not** the console-script
  wrappers (see `docs/troubleshooting.md` for the ostree/SELinux reason).
- If a change is user-facing, add a line to `CHANGELOG.md` under `[Unreleased]`.

## Reporting hardware results

Confirming that a specific TV model + webOS version works (or doesn't) is
genuinely useful — open an issue with the details from the bug-report form even
if it's good news.
