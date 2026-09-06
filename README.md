# DDJ System EQ

Turn a Pioneer / AlphaTheta **DDJ** controller into a system-wide **isolator
EQ and master fader** for everything Windows plays — YouTube, SoundCloud,
Spotify, games, anything — without launching rekordbox.

- **HI / MID / LOW** knobs of one mixer channel → 3-band EQ on your output
  device, with the two curves of a DJM-A9's *EQ CURVE* switch: **ISOLATOR**
  (−∞…+6 dB, bands drop out) or **EQ** (−26…+6 dB) — switchable from the tray.
- **Channel fader** → master volume (log taper, mute at the bottom).
- **CFX / FILTER knob** → DJ filter: left sweeps a low-pass down to 80 Hz,
  right sweeps a high-pass up to 8 kHz, centre is off. 24 dB/oct with mild
  resonance (the resonance is a config key, like the DJM-A9's PARAMETER knob).
- **Steps aside for DJ software.** The moment rekordbox, Serato, djay,
  VirtualDJ or Traktor starts, the bridge releases the controller and
  flattens the EQ; when it exits, everything comes back.
- Runs from the **system tray**: pick the controller, the mixer channel
  (1–4), the EQ mode, bypass, or **MIDI-learn** the knobs on an unusual unit.
- Zero added latency: the DSP is [Equalizer APO](https://sourceforge.net/projects/equalizerapo/),
  which lives inside the Windows audio engine. The bridge just tells it what
  to do.

Tray colours: **green** active · **blue** bypassed · **purple** MIDI learn ·
**amber** yielded to DJ software · **grey** waiting for controller ·
**red** can't write APO's config (see log).

## Supported controllers

Every Pioneer/AlphaTheta controller uses the same mixer-section MIDI layout
(strip *N* on MIDI channel *N*, HI/MID/LOW = CC 7/11/15, fader = CC 19, with
fine-resolution pairs at +32), so this works out of the box on most of them.

| Model | Mixer channels | Status |
|---|---|---|
| DDJ-200 | 2 | **Tested** (USB; not Bluetooth) |
| DDJ-400, DDJ-FLX4, DDJ-FLX2, DDJ-SB3, DDJ-800 | 2 | Expected to work — please report |
| DDJ-FLX6, DDJ-1000 | 4 | Expected to work — please report |
| DDJ-FLX10, Opus-Quad, other 2024+ AlphaTheta units | 4 | **Not yet**: these stay silent until DJ software sends a wake-up handshake |
| Anything else with "DDJ", "XDJ" or "DJM" in its port name | 4 offered | Try it; use *MIDI learn* if a knob is on a different CC |

Own one of the untested ones? Run `--monitor` (below) and open a
[controller report](../../issues/new?template=controller-report.md).

## 1. Prerequisites

| Item | Notes |
|---|---|
| Windows 10/11 | |
| [Equalizer APO](https://sourceforge.net/projects/equalizerapo/) | Install, then in **Configurator** tick every output device you listen through. Reboot once. Bluetooth outputs usually can't host APO. |
| The controller on **USB** | No vendor driver needed for the MIDI side. |

No Python needed if you use the release executables.

## 2. Install

Download `DDJ200Bridge.exe` (tray app) and `DDJ200Bridge-cli.exe` (console
helper) from **Releases** into a folder of your choice, e.g.
`C:\Tools\ddj-system-eq\dist\`. *(The exe/task/folder are still named
`DDJ200Bridge` for compatibility with the original build; they are the same
program for every model.)*

**Equalizer APO wiring is automatic.** On first start the bridge creates
`…\EqualizerAPO\config\ddj200.txt` and adds `Include: ddj200.txt` to
`config.txt` (and re-checks every couple of seconds, so Peace rewriting
`config.txt` can't break it). If you use Peace, its curve stays as the
baseline and the DDJ EQ stacks on top.

If the tray icon is **red**, your account can't write to APO's config folder
(plain APO install without Peace). Run once from an **Administrator**
PowerShell:

```bash
powershell -ExecutionPolicy Bypass -File .\setup-apo.ps1
```

## 3. Check the controller (1 minute)

Close any DJ software, plug the controller in, then:

```bash
.\dist\DDJ200Bridge-cli.exe --monitor
```

Turn the HI, MID and LOW knobs and move the fader of the mixer channel you
want to use. Expect `ch N  CC 7 / 11 / 15` and `CC 19` (plus `CC 39/43/47/51`
fine-resolution pairs). The CFX knob shows as `ch 7  CC 23` (deck 1) or
`CC 24` (deck 2). Different numbers? Use **MIDI learn** in the tray later —
no editing needed. Ctrl+C to stop.

## 4. First run

```bash
.\dist\DDJ200Bridge-cli.exe --console
```

Play something, turn the knobs. Centre detent = flat, fully left = kill,
fully right = boost. Launch rekordbox: the console prints *Yielded* within
about a second and the controller works normally there. Quit it: *Waiting
for controller* → *Active*.

## 5. Run at logon

```bash
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1 -StartNow
```

Registers a per-user Scheduled Task (at logon, 15 s delay) and starts the
tray app now. Quit from the tray icon. `-Remove` unregisters.

Windows SmartScreen may query the unsigned exe on first run — "Run anyway",
or build it yourself (below). Two `DDJ200Bridge.exe` entries in Task Manager
are normal (PyInstaller launcher + app).

## Tray menu

```
● Active – DDJ-400, channel 2, isolator
───────────────
Controller      ▸  DDJ-400  –  DDJ-400 ✓ / …
Mixer channel   ▸  Channel 1 / Channel 2 ✓ / (3 / 4 on 4-channel decks)
EQ mode         ▸  ISOLATOR (DJM-A9 curve, −∞..+6 dB) ✓ / EQ (DJM-A9 curve, −26..+6 dB)
Filter resonance ▸ None (Q 0.7) / Mild (Q 1.0) ✓ / Medium (Q 1.4) / Strong (Q 2.0)
Fader curve     ▸  Concave (rises near the top) / Linear ✓ / Early ramp (rises near the bottom)
LPF Frequency Adjustment (Headphone Mode) ▸ −15 / −10 / −5 / Normal ✓ / +5 / +10 / +15 Hz
Bypass EQ          (toggle)
MIDI learn      ▸  HI knob / MID knob / LOW knob / Fader / Filter-CFX knob
Reset EQ to flat
───────────────
Open config folder · Open log · About · Quit
```

Everything chosen here is saved to `%LOCALAPPDATA%\DDJ200Bridge\config.json`
and applies immediately, no restart.

## Tuning

Edit `config.json` (UTF-8, restart the bridge after hand edits).

| Key | Default | What it does |
|---|---|---|
| `eq_mode` | `isolator` | `isolator` or `eq`; each mode's curve lives under `eq_modes` |
| `eq_modes.isolator.kill_db` / `boost_db` | −60 / +6 | DJM-A9 ISOLATOR range (−∞ rendered as −60 dB, band effectively gone) |
| `eq_modes.eq.kill_db` / `boost_db` | −26 / +6 | DJM-A9 EQ range (Pioneer specifies it at 20 kHz / 1 kHz / 20 Hz) |
| `eq_modes.*.bands.*.fc` | iso 200 / 1000 / 5000 Hz · eq 120 / 1000 / 8000 Hz | Low-shelf corner / mid centre / high-shelf corner. Pioneer doesn't publish corner frequencies; these are chosen to match the specified ranges |
| `eq_modes.*.bands.*.stages` | 3 (isolator) / 1 (eq) | Cascaded filters per band; 3 × 12 dB/oct = 36 dB/oct |
| `eq_modes.*.auto_preamp` | off | When on, the preamp drops by the largest boost so flat = bypass loudness (makes boosts feel weak). Keep Windows volume under 100 % when off |
| `fader_min_db` | −60 | Quietest level just above the fader's bottom stop |
| `low_fc_offset_hz` | 0 | *LPF Frequency Adjustment (Headphone Mode)*: shifts the LOW band's corner from the mode's default (isolator 200 Hz / eq 120 Hz) in 5 Hz steps, −15…+15 |
| `fader_curve` | `linear` | Like the DJM-A9's CH FADER CURVE switch: `concave` (level arrives near the top), `linear` (audio taper, −6 dB at half), `early_ramp` (near full by a third of the way up — for cuts) |
| `filter_q` | 1.0 | Filter resonance (tray → *Filter resonance* on units without a PARAMETER knob): 0.707 none, 1.0 mild, 1.4 medium, 2.0 strong — watch headroom |
| `filter_stages` | 2 | 12 dB/oct per stage; 2 = 24 dB/oct |
| `filter_lp_min_hz` / `filter_hp_max_hz` | 80 / 8000 | Cutoff reached at full left / full right |
| `filter_channel` / `filter_cc_base` | 7 / 23 | Where the CFX knob lives; CC = base + strip − 1. `filter_cc` (set by MIDI learn) overrides |
| `max_filter_step_per_tick` | 0.12 | Filter glide speed (fraction of travel per update) |
| `max_db_step_per_tick` / `write_rate_hz` | 12 dB / 40 | Anti-click ramp. Lower the step if you hear clicks on fast sweeps |
| `dj_software_process_names` | rekordbox, Serato, djay, VirtualDJ, Traktor | Any of these running → bridge yields |
| `restore_on_start` | true | Re-apply the last curve on start; knobs are absolute, so the first touch snaps to the physical position |

## Build from source

```bash
python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller; powershell -ExecutionPolicy Bypass -File .\build.ps1
```

Python 3.10+. MIDI is read through Windows' own WinMM API (`winmidi.py`) and
processes through Toolhelp (`winproc.py`) via ctypes, so the only packages
are `pystray` and `Pillow` for the tray icon. Tagged pushes build the exes on
GitHub Actions and attach them to the release.

## Troubleshooting

- **EQ has no effect but the file changes** (green icon, `ddj200.txt`
  updates when you turn a knob, sound unchanged) — Equalizer APO isn't
  installed on the output device in use. Open **Equalizer APO →
  Configurator** (admin), tick the device you actually listen through, OK,
  reboot. A driver or Windows update can silently drop this registration
  even if Peace is installed.
- **APO is ticked but still does nothing** (test: write `Preamp: -60 dB`
  into `ddj200.txt` by hand — no change in loudness) — some drivers, the
  Focusrite USB driver among them, ignore APO's default SFX/EFX install slots
  and only honour LFX/GFX. From an admin PowerShell:
  `.\apo-endpoint-lfx.ps1 -List` to see your devices, then
  `.\apo-endpoint-lfx.ps1 -Device "Speakers"` (part of the name). It backs
  up the registry keys, switches the slots, restarts Windows Audio;
  `-Revert` undoes it. Pause/resume playback afterwards.
- **Grey icon with the controller plugged in** — another app holds the MIDI
  port (DJ software not in the yield list, a browser tab using Web MIDI…),
  or the port name doesn't contain "DDJ" (`--list-ports`; set
  `midi_port_match`).
- **A knob does nothing / wrong knob** — tray → *MIDI learn* → pick the
  control → turn it. Saved instantly.
- **No tray icon but the log says active** — the bridge is running headless
  (the tray is best-effort). Quit via Task Manager.
- **Clicks on fast knob moves** — lower `max_db_step_per_tick`.
- **Config edited but ignored** — it must be UTF-8; the log will say
  "Config unreadable" if it isn't valid JSON.

Log: `%LOCALAPPDATA%\DDJ200Bridge\bridge.log`. Early-start breadcrumbs:
`%TEMP%\DDJ200Bridge-boot.log`.

## How it works

`bridge.py` opens the controller's MIDI input, maps the chosen strip's three
EQ knobs and fader to decibels, ramps the live values a few dB per update to
avoid clicks, and rewrites a five-to-eleven-line Equalizer APO include file
at up to 40 Hz. APO reloads it on the file-change notification. A supervisor
thread watches the process list every 0.5 s; when DJ software appears it
closes the port and writes a flat file, and reverses that when the software
exits. Nothing in the audio path is added: APO already sits in the Windows
audio engine, so the EQ costs no latency and video stays in sync.

## Licence

MIT.
