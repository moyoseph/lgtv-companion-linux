# Security policy

## Supported versions

Only the latest released version receives fixes. Please upgrade before
reporting (`pip install --upgrade lgtvcompanion`).

## Reporting a vulnerability

Please report security issues **privately** rather than opening a public issue:

- Use GitHub's [private vulnerability reporting](https://github.com/moyoseph/lgtv-companion-linux/security/advisories/new)
  (Security → Report a vulnerability), or
- email the maintainer.

I'll acknowledge as soon as I can and keep you posted on a fix. Thanks for
disclosing responsibly.

## Scope notes

This tool controls TVs on your local network over the LG SSAP protocol
(`wss://<tv>:3001`). Pairing keys are stored locally (`/var/lib/lgtv-companion/keys/`
in system mode, `~/.local/state/lgtv-companion/` in user mode). It performs no
outbound calls except the optional, opt-in daily GitHub release check.
