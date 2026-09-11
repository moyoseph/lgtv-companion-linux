# Migration

## From the Windows LGTV Companion

Copy your Windows `config.json` (from `%ProgramData%\LGTV Companion\`) to the
Linux box and run:

```sh
sudo lgtvc setup import-windows --file config.json
```

This converts the settings to the Linux schema, moves each device's
`SessionKey` into the key store (so **no re-pairing**), and maps what it can:

| Windows | Linux |
|---|---|
| `PowerOnTimeOut`, `BlankWhenIdle*`, `MuteSpeakers`, `RemoteStream*`, topology flags | direct equivalents |
| `IgnoredKeysList` (VK codes) | translated to evdev key codes (media keys; others dropped with a warning) |
| `BlankWhenIdleProcessList` | `idle.process_list` (binary + flags) |
| per-device `IP`/`MAC`/`WOL`/`SourceHdmiInput`/`CheckHdmiInputWhenPoweringOff`/`SetHdmiInput*`/`PersistentConnectionLevel` | direct equivalents |
| `NicLuid` | dropped — set `interface` to a NIC name if you need source binding |
| `TimingShutdown`, `UpdaterMode`, RDP detection, locale word lists | not applicable on Linux |

## From a hand-rolled `/etc/lgtvcontrol` setup

If you already had the small `lgtv.py`-based setup:

```sh
sudo lgtvc setup import-legacy          # copies client.key + writes config (dry_run on)
# soak / verify, then:
sudo lgtvc setup migrate-legacy --legacy-user "$USER"   # disables the old units, enables ours
```

`migrate-legacy` disables `lgtv-startup`/`lgtv-sleep`/`lgtv-shutdown` and the
user `tv-wake-on-input` unit, flips `dry_run` off, and enables the `lgtvc-*`
units. Nothing is deleted.

### Rollback

```sh
sudo lgtvc setup rollback-legacy --legacy-user "$USER"
```

Re-enables the legacy units and disables ours. The client key was copied (not
moved), so the old stack works again immediately.
