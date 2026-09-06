#!/usr/bin/env python3
"""
DDJ -> Equalizer APO bridge: any Pioneer / AlphaTheta DDJ controller becomes a
system-wide isolator EQ (HI / MID / LOW) plus a master fader on Windows.

The bridge rewrites one Equalizer APO include file. Whenever DJ software
(rekordbox, Serato, djay, VirtualDJ, Traktor) is running it steps aside:
releases the MIDI port and flattens the EQ; it comes back when that exits.

States
    active     bridge owns the controller, EQ engaged on system audio
    yielded    DJ software running: MIDI port released, EQ flat
    bypassed   user switched the EQ off from the tray
    no_device  no controller found (or port busy)
    error      cannot write the APO include file (see log)

Usage
    bridge.py                 run with tray icon (normal)
    bridge.py --console       run in the console instead of the tray
    bridge.py --monitor       print MIDI from the controller (mapping check)
    bridge.py --list-ports    list MIDI input ports
    bridge.py --flat          write a flat include file and exit
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import math
import os
import re
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import winmidi
from winproc import running_process_names

APP_NAME = "DDJ200Bridge"
APP_VERSION = "1.4.0"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
LOG_PATH = APP_DIR / "bridge.log"

BANDS = ("low", "mid", "hi")
FADER = "fader"
FILTER = "filter"
CONTROLS = BANDS + (FADER, FILTER)
LEARN_TIMEOUT_S = 20

# Known controllers: how many mixer channels the tray should offer. Every one
# of these uses the same mixer MIDI layout (strip N on MIDI channel N,
# HI/MID/LOW = CC 7/11/15, fader = CC 19, fine-resolution pair at +32), so the
# profile only needs the channel count. First match wins; the generic entry
# catches the rest.
PROFILES = [
    {"name": "DDJ-200", "match": r"DDJ-200", "channels": 2},
    {"name": "DDJ-400", "match": r"DDJ-400", "channels": 2},
    {"name": "DDJ-FLX4", "match": r"DDJ-FLX4", "channels": 2},
    {"name": "DDJ-FLX2", "match": r"DDJ-FLX2", "channels": 2},
    {"name": "DDJ-SB3", "match": r"DDJ-SB3", "channels": 2},
    {"name": "DDJ-800", "match": r"DDJ-800", "channels": 2},
    {"name": "DDJ-FLX6", "match": r"DDJ-FLX6", "channels": 4},
    {"name": "DDJ-1000", "match": r"DDJ-1000", "channels": 4},
    {"name": "DDJ-FLX10", "match": r"DDJ-FLX10", "channels": 4},
    {"name": "Pioneer / AlphaTheta (generic)", "match": r"DDJ|XDJ|DJM", "channels": 4},
]

DEFAULT_CONFIG = {
    # Regex (case-insensitive) that a MIDI input port name must contain.
    "midi_port_match": "DDJ",
    # Exact port name chosen in the tray; "" = first port matching the regex.
    "midi_port_name": "",
    # 1-based mixer channel (strip) whose knobs/fader are used.
    "midi_channel": 1,
    # Pioneer convention: HI=7, MID=11, LOW=15, fader=19, LSB at CC+32.
    # "MIDI learn" in the tray rewrites these for an odd controller.
    "cc": {"hi": 7, "mid": 11, "low": 15},
    "lsb_offset": 32,
    "fader_enabled": True,
    "fader_cc": 19,
    # Fader at the bottom mutes; just above it starts at fader_min_db.
    "fader_min_db": -60.0,
    # Fader curve like the DJM-A9's CH FADER CURVE switch: "concave" rises
    # mostly near the top, "linear" is a plain audio taper, "early_ramp"
    # rises sharply near the bottom (for cuts/transforms).
    "fader_curve": "linear",
    # CFX / FILTER knob -> DJ filter: left = low-pass sweeping down, right =
    # high-pass sweeping up, centre = off. On DDJ-200/400/FLX4 the CFX knobs
    # sit on MIDI channel 7, CC 23 (deck 1) / 24 (deck 2): cc = base + strip-1.
    # "MIDI learn" fills filter_cc/filter_channel for other layouts.
    "filter_enabled": True,
    "filter_channel": 7,
    "filter_cc_base": 23,
    "filter_cc": None,
    # Two cascaded 12 dB/oct stages = 24 dB/oct. Q sets resonance like the
    # DJM-A9's PARAMETER knob: 0.707 = none, 1.0 = mild, 1.5+ = strong.
    "filter_stages": 2,
    "filter_q": 1.0,
    "filter_lp_min_hz": 80,
    "filter_hp_max_hz": 8000,
    # Fraction of the knob's travel the filter may glide per APO update.
    "max_filter_step_per_tick": 0.12,
    # Fraction of knob travel either side of the centre detent that is 0 dB.
    "center_deadband": 0.02,
    # "Headphone mode": shifts the LOW band's corner frequency from the mode's
    # default (isolator 200 Hz / eq 120 Hz) in 5 Hz steps, -15..+15.
    "low_fc_offset_hz": 0,
    # Which response the knobs have. Switchable from the tray - the two modes
    # mirror the DJM-A9's [EQ CURVE] switch (EQ / ISOLATOR).
    "eq_mode": "isolator",
    "eq_modes": {
        # DJM-A9 "ISOLATOR" curve: each band -inf..+6 dB. -inf is rendered as
        # -60 dB split over three cascaded 12 dB/oct filters (36 dB/oct), so a
        # band drops out rather than being turned down.
        "isolator": {
            "kill_db": -60.0, "boost_db": 6.0, "kill_curve": 1.5, "auto_preamp": False,
            "bands": {
                "low": {"type": "LS", "fc": 200, "stages": 3},
                "mid": {"type": "PK", "fc": 1000, "q": 0.5, "stages": 3},
                "hi": {"type": "HS", "fc": 5000, "stages": 3},
            },
        },
        # DJM-A9 "EQ" curve: HI/MID/LOW -26..+6 dB (Pioneer specifies the
        # range at 20 kHz / 1 kHz / 20 Hz); single shelves and a broad mid.
        "eq": {
            "kill_db": -26.0, "boost_db": 6.0, "kill_curve": 1.0, "auto_preamp": False,
            "bands": {
                "low": {"type": "LS", "fc": 120, "stages": 1},
                "mid": {"type": "PK", "fc": 1000, "q": 0.6, "stages": 1},
                "hi": {"type": "HS", "fc": 8000, "stages": 1},
            },
        },
    },
    "apo_config_dir": r"C:\Program Files\EqualizerAPO\config",
    "apo_include_file": "ddj200.txt",
    # Any of these running => the bridge yields (port released, EQ flat).
    "dj_software_process_names": [
        "rekordbox.exe", "Serato DJ Pro.exe", "Serato DJ Lite.exe",
        "djay Pro.exe", "djay.exe", "VirtualDJ.exe", "Traktor.exe",
    ],
    "process_poll_seconds": 0.5,
    "device_poll_seconds": 2.0,
    # APO reload rate and the largest gain jump per reload (anti-click).
    "write_rate_hz": 40,
    "max_db_step_per_tick": 12.0,
    # Re-apply the last EQ curve when the bridge starts / regains the device.
    "restore_on_start": True,
}

log = logging.getLogger(APP_NAME)

# Early-boot breadcrumbs: written before logging exists, so a windowed build
# that dies or stalls during start-up still leaves evidence somewhere fixed.
BOOT_LOG = Path(tempfile.gettempdir()) / "DDJ200Bridge-boot.log"


def _crumb(msg: str) -> None:
    try:
        with open(BOOT_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def _excepthook(exc_type, exc, tb) -> None:
    _crumb("UNHANDLED: " + "".join(traceback.format_exception(exc_type, exc, tb)))
    try:
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    except Exception:  # noqa: BLE001
        pass


sys.excepthook = _excepthook


# --------------------------------------------------------------------------- #
# Config / state files
# --------------------------------------------------------------------------- #
def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        save_config(dict(DEFAULT_CONFIG), path)
        log.info("Wrote default config to %s", path)
        return dict(DEFAULT_CONFIG)
    try:
        # utf-8-sig: tolerate the BOM that PowerShell 5.1 / Notepad may add.
        user = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        log.error("Config %s unreadable (%s); using defaults", path, exc)
        return dict(DEFAULT_CONFIG)
    # v1.0 name for the yield list; v1.2.0 name for the EQ curve.
    if "rekordbox_process_names" in user and "dj_software_process_names" not in user:
        user["dj_software_process_names"] = user.pop("rekordbox_process_names")
    if user.get("eq_mode") == "gentle":
        user["eq_mode"] = "eq"
    # v1.3.2 fader-curve names.
    renames = {"gradual": "concave", "even": "linear", "fast": "early_ramp"}
    if user.get("fader_curve") in renames:
        user["fader_curve"] = renames[user["fader_curve"]]
    return _merge(DEFAULT_CONFIG, user)


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:  # no BOM
            json.dump(cfg, fh, indent=2)
    except OSError as exc:
        log.warning("Could not save config: %s", exc)


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        return {c: float(data.get(c, 0.0)) for c in CONTROLS}
    except Exception:  # noqa: BLE001
        return {c: 0.0 for c in CONTROLS}


def save_state(gains: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(gains), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not save state: %s", exc)


def profile_for(port_name: str) -> dict:
    for p in PROFILES:
        if re.search(p["match"], port_name, re.IGNORECASE):
            return p
    return PROFILES[-1]


# --------------------------------------------------------------------------- #
# Equalizer APO include-file writer
# --------------------------------------------------------------------------- #
class ApoWriter:
    """Holds target gains, ramps the live gains towards them, writes APO config."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.path = Path(cfg["apo_config_dir"]) / cfg["apo_include_file"]
        self.target = {c: 0.0 for c in CONTROLS}
        self.current = {c: 0.0 for c in CONTROLS}
        self.bypassed = True
        self.dirty_state = False
        self.last_error: str | None = None
        self._written: str | None = None
        self._lock = threading.Lock()

    def mode(self) -> dict:
        modes = self.cfg["eq_modes"]
        return modes.get(self.cfg["eq_mode"]) or modes["isolator"]

    # -- gain mapping ------------------------------------------------------- #
    def knob_to_db(self, value14: int) -> float:
        m = self.mode()
        x = max(0, min(16383, value14)) / 16383.0
        d = x - 0.5
        dead = float(self.cfg["center_deadband"])
        if abs(d) <= dead:
            return 0.0
        span = 0.5 - dead
        if d < 0:
            frac = (-d - dead) / span
            return float(m["kill_db"]) * (frac ** float(m.get("kill_curve", 1.0)))
        frac = (d - dead) / span
        return float(m["boost_db"]) * frac

    FADER_CURVES = {"concave": 3.0, "linear": 1.0, "early_ramp": 1.0 / 3.0}

    def fader_to_db(self, value14: int) -> float:
        """Fader top = 0 dB, curved taper down to fader_min_db, bottom = mute."""
        x = max(0, min(16383, value14)) / 16383.0
        if x < 0.005:
            return -100.0
        x = x ** self.FADER_CURVES.get(str(self.cfg.get("fader_curve", "linear")), 1.0)
        return max(float(self.cfg["fader_min_db"]), 20.0 * math.log10(x))

    @staticmethod
    def knob_to_position(value14: int) -> float:
        """Filter knob as -1 (full left) .. 0 (centre) .. +1 (full right)."""
        return max(0, min(16383, value14)) / 16383.0 * 2.0 - 1.0

    def filter_lines(self, pos: float) -> list[str]:
        """APO filter lines for a knob position; [] when centred."""
        dead = float(self.cfg["center_deadband"])
        if abs(pos) <= dead or not self.cfg.get("filter_enabled", True):
            return []
        frac = (abs(pos) - dead) / (1.0 - dead)  # 0..1 of the usable travel
        if pos < 0:  # low-pass: 20 kHz down to lp_min (log sweep)
            lo = float(self.cfg["filter_lp_min_hz"])
            fc = 20000.0 * (lo / 20000.0) ** frac
            kind = "LPQ"
        else:        # high-pass: 20 Hz up to hp_max (log sweep)
            hi = float(self.cfg["filter_hp_max_hz"])
            fc = 20.0 * (hi / 20.0) ** frac
            kind = "HPQ"
        line = f"Filter: ON {kind} Fc {fc:.0f} Hz Q {float(self.cfg['filter_q']):.3f}"
        return [line] * max(1, int(self.cfg["filter_stages"]))

    # -- public API --------------------------------------------------------- #
    def set_target(self, control: str, db: float) -> None:
        with self._lock:
            if self.target[control] != db:
                self.target[control] = db
                self.dirty_state = True

    def set_all_targets(self, gains: dict) -> None:
        with self._lock:
            for c in CONTROLS:
                self.target[c] = float(gains.get(c, 0.0))
            self.dirty_state = True

    def bypass(self, reason: str = "bypassed - DJ software is running") -> None:
        """Flatten immediately. Targets are kept for resume()."""
        with self._lock:
            self.bypassed = True
            self.current = {c: 0.0 for c in CONTROLS}
        self._write(self._render_flat(reason))

    def resume(self) -> None:
        """Re-engage; gains ramp from flat to the stored targets on next ticks."""
        with self._lock:
            self.bypassed = False
            self.current = {c: 0.0 for c in CONTROLS}

    def force_rewrite(self) -> None:
        """Re-render with the current mode (used after an EQ-mode switch)."""
        with self._lock:
            if self.bypassed:
                return
            content = self._render(self.current)
        self._written = None
        self._write(content)

    def write_flat_now(self) -> None:
        self._write(self._render_flat("flat"))

    def ensure_include(self) -> None:
        """Make sure APO's config.txt includes our file (Peace rewrites config.txt)."""
        config_txt = self.path.parent / "config.txt"
        line = f"Include: {self.path.name}"
        try:
            text = config_txt.read_text(encoding="utf-8", errors="replace") if config_txt.exists() else ""
            if any(l.strip().lower() == line.lower() for l in text.splitlines()):
                return
            with open(config_txt, "a", encoding="utf-8") as fh:
                if text and not text.endswith(("\n", "\r\n")):
                    fh.write("\n")
                fh.write(line + "\n")
            log.info("Added '%s' to %s", line, config_txt)
        except OSError as exc:
            if self.last_error != str(exc):
                log.error("Cannot update %s: %s (run setup-apo.ps1 as admin)", config_txt, exc)
            self.last_error = str(exc)

    def tick(self) -> None:
        """Move current gains one step towards target and write if changed."""
        with self._lock:
            if self.bypassed:
                return
            step = float(self.cfg["max_db_step_per_tick"])
            fstep = float(self.cfg["max_filter_step_per_tick"])
            changed = False
            for c in CONTROLS:
                delta = self.target[c] - self.current[c]
                eps = 0.002 if c == FILTER else 0.05
                if abs(delta) < eps:
                    if self.current[c] != self.target[c]:
                        self.current[c] = self.target[c]
                        changed = True
                    continue
                # Fader spans a much wider dB range; the filter moves in knob
                # position (-1..1), which is a log-frequency glide.
                lim = fstep if c == FILTER else (step * 3 if c == FADER else step)
                self.current[c] += max(-lim, min(lim, delta))
                changed = True
            if not changed:
                return
            content = self._render(self.current)
        self._write(content)

    def snapshot_targets(self) -> dict:
        with self._lock:
            self.dirty_state = False
            return dict(self.target)

    # -- rendering / IO ----------------------------------------------------- #
    def _render(self, gains: dict) -> str:
        m = self.mode()
        preamp = gains.get(FADER, 0.0)
        if m.get("auto_preamp"):
            preamp -= max(0.0, max(gains[b] for b in BANDS))
        if preamp == 0:
            preamp = 0.0  # avoid "-0.0"
        lines = [
            f"# Generated by {APP_NAME} ({self.cfg['eq_mode']}) - do not edit by hand",
            f"Preamp: {preamp:.1f} dB",
        ]
        for b in BANDS:
            spec = m["bands"][b]
            ftype = spec["type"]
            stages = max(1, int(spec.get("stages", 1)))
            per_stage = gains[b] / stages + 0.0
            fc = float(spec["fc"])
            if b == "low":
                fc = max(20.0, fc + float(self.cfg.get("low_fc_offset_hz", 0)))
            line = f"Filter: ON {ftype} Fc {fc:g} Hz Gain {per_stage:.1f} dB"
            if ftype in ("PK", "LSC", "HSC", "LPQ", "HPQ"):
                line += f" Q {float(spec.get('q', 0.7)):.2f}"
            lines.extend([line] * stages)
        lines.extend(self.filter_lines(gains.get(FILTER, 0.0)))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_flat(reason: str) -> str:
        return f"# Generated by {APP_NAME} - {reason}\n"

    def _write(self, content: str) -> None:
        if content == self._written:
            return
        try:
            # Single small write; APO reloads on the change notification.
            with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            self._written = content
            if self.last_error:
                log.info("APO include file writable again")
                self.last_error = None
        except OSError as exc:
            if self.last_error != str(exc):
                log.error("Cannot write %s: %s (run setup-apo.ps1 as admin?)", self.path, exc)
            self.last_error = str(exc)


# --------------------------------------------------------------------------- #
# MIDI input
# --------------------------------------------------------------------------- #
class MidiInput:
    def __init__(self, cfg: dict, on_knob, on_raw=None):
        self.cfg = cfg
        self.on_knob = on_knob
        self.on_raw = on_raw  # (status, data1, data2) before mapping - MIDI learn
        self.port = None
        self.name: str | None = None
        self._msb: dict[str, int] = {}
        self.reconfigure()

    def reconfigure(self) -> None:
        """Rebuild the CC lookup from cfg (channel / cc map may have changed)."""
        cfg = self.cfg
        self.channel = int(cfg["midi_channel"]) - 1
        cc = cfg["cc"]
        lsb = int(cfg["lsb_offset"])
        self.msb_map = {int(cc[b]): b for b in BANDS}
        self.lsb_map = {int(cc[b]) + lsb: b for b in BANDS}
        if cfg.get("fader_enabled", True):
            self.msb_map[int(cfg["fader_cc"])] = FADER
            self.lsb_map[int(cfg["fader_cc"]) + lsb] = FADER
        # The CFX knob lives on its own MIDI channel (7-bit, no LSB pair).
        self.filter_channel = int(cfg.get("filter_channel", 7)) - 1
        override = cfg.get("filter_cc")
        self.filter_cc = int(override) if override is not None else int(cfg["filter_cc_base"]) + self.channel
        self._msb.clear()

    @staticmethod
    def list_names() -> list[str]:
        try:
            return winmidi.list_inputs()
        except Exception as exc:  # noqa: BLE001
            log.warning("MIDI enumeration failed: %s", exc)
            return []

    def candidates(self) -> list[tuple[int, str]]:
        """All input ports matching midi_port_match, as (index, name)."""
        pat = re.compile(str(self.cfg["midi_port_match"]), re.IGNORECASE)
        return [(i, n) for i, n in enumerate(self.list_names()) if pat.search(n)]

    def find(self) -> tuple[int, str] | None:
        cands = self.candidates()
        if not cands:
            return None
        wanted = str(self.cfg.get("midi_port_name") or "")
        for i, n in cands:
            if wanted and n == wanted:
                return i, n
        return cands[0]

    def open(self) -> bool:
        found = self.find()
        if not found:
            return False
        idx, name = found
        try:
            self.port = winmidi.MidiIn(idx, self._callback)
        except winmidi.MidiError as exc:
            why = "in use by another app" if exc.in_use else str(exc)
            log.warning("Could not open '%s': %s", name, why)
            self.port = None
            return False
        self.name = name
        self._msb.clear()
        log.info("Opened MIDI input '%s' (%s, mixer channel %d)",
                 name, profile_for(name)["name"], self.channel + 1)
        return True

    def close(self) -> None:
        if self.port is not None:
            try:
                self.port.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("Released MIDI input '%s'", self.name)
        self.port = None
        self.name = None

    @property
    def is_open(self) -> bool:
        return self.port is not None

    def still_present(self) -> bool:
        return self.name is not None and self.name in self.list_names()

    def _callback(self, status: int, control: int, value: int) -> None:
        try:
            if self.on_raw is not None:
                self.on_raw(status, control, value)
            # 0xB0..0xBF = control change on channel (status & 0x0F)
            if (status & 0xF0) != 0xB0:
                return
            ch = status & 0x0F
            if (self.cfg.get("filter_enabled", True) and ch == self.filter_channel
                    and control == self.filter_cc):
                self.on_knob(FILTER, value << 7)
                return
            if ch != self.channel:
                return
            if control in self.msb_map:
                band = self.msb_map[control]
                self._msb[band] = value
                self.on_knob(band, value << 7)
            elif control in self.lsb_map:
                band = self.lsb_map[control]
                self.on_knob(band, (self._msb.get(band, 0) << 7) | value)
        except Exception as exc:  # noqa: BLE001
            log.exception("MIDI callback error: %s", exc)


# --------------------------------------------------------------------------- #
# DJ-software process watcher (one Toolhelp snapshot per poll, ~1 ms)
# --------------------------------------------------------------------------- #
class ProcessWatcher:
    def __init__(self, names: list[str]):
        self.names = {n.lower() for n in names}

    def poll(self) -> bool:
        try:
            return not self.names.isdisjoint(running_process_names())
        except OSError as exc:
            log.warning("Process snapshot failed: %s", exc)
            return False


# --------------------------------------------------------------------------- #
# Bridge state machine
# --------------------------------------------------------------------------- #
class Bridge:
    def __init__(self, cfg: dict, config_path: Path = CONFIG_PATH):
        self.cfg = cfg
        self.config_path = config_path
        self.apo = ApoWriter(cfg)
        self.midi = MidiInput(cfg, self._on_knob, self._on_raw)
        self.procs = ProcessWatcher(cfg["dj_software_process_names"])
        self.state = "starting"
        self.user_bypass = False
        self.learning: str | None = None
        self._learn_deadline = 0.0
        self.raw: dict[str, int] = {}  # last 14-bit value per control
        self.on_state = None  # callback(state)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        # Probe the APO files immediately so a permissions problem shows up
        # (red icon) at startup rather than on the first knob movement.
        self.apo.write_flat_now()
        self.apo.ensure_include()
        if self.cfg["restore_on_start"]:
            self.apo.set_all_targets(load_state())
        self._threads = [
            threading.Thread(target=self._supervisor, name="supervisor", daemon=True),
            threading.Thread(target=self._writer, name="apo-writer", daemon=True),
        ]
        for t in self._threads:
            t.start()
        log.info("Bridge %s started", APP_VERSION)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)
        self.midi.close()
        # Leave the system clean: no EQ once we're gone.
        self.apo.bypass("flat - bridge not running")
        save_state(self.apo.snapshot_targets())
        log.info("Bridge stopped")

    # -- tray actions ------------------------------------------------------- #
    def reset_flat(self) -> None:
        self.apo.set_all_targets({c: 0.0 for c in CONTROLS})
        log.info("EQ reset to flat")

    def channels_available(self) -> int:
        name = self.midi.name or (self.midi.find() or (0, ""))[1]
        return int(profile_for(name)["channels"]) if name else 4

    def set_channel(self, n: int) -> None:
        if n == int(self.cfg["midi_channel"]):
            return
        self.cfg["midi_channel"] = int(n)
        self.cfg["filter_cc"] = None  # follow the strip again (base + strip-1)
        self.midi.reconfigure()
        save_config(self.cfg, self.config_path)
        log.info("Mixer channel -> %d", n)
        self._notify()

    def set_port(self, name: str) -> None:
        if name == self.cfg.get("midi_port_name"):
            return
        self.cfg["midi_port_name"] = name
        save_config(self.cfg, self.config_path)
        log.info("Controller -> '%s'", name)
        if self.state == "active" and self.midi.name != name:
            self.midi.close()
            self._set_state("no_device")  # supervisor reopens the chosen port

    def set_mode(self, mode: str) -> None:
        if mode not in self.cfg["eq_modes"] or mode == self.cfg["eq_mode"]:
            return
        self.cfg["eq_mode"] = mode
        save_config(self.cfg, self.config_path)
        # Re-map the knobs' last positions through the new curve.
        for band in BANDS:
            if band in self.raw:
                self.apo.set_target(band, self.apo.knob_to_db(self.raw[band]))
        self.apo.force_rewrite()
        log.info("EQ mode -> %s", mode)
        self._notify()

    def set_filter_q(self, q: float) -> None:
        """Filter resonance (the DJM-A9's PARAMETER knob) for units without one."""
        if abs(float(self.cfg["filter_q"]) - q) < 1e-6:
            return
        self.cfg["filter_q"] = float(q)
        save_config(self.cfg, self.config_path)
        self.apo.force_rewrite()
        log.info("Filter resonance -> Q %.2f", q)
        self._notify()

    def set_low_offset(self, hz: int) -> None:
        """Headphone mode: shift the LOW band's corner by hz (5 Hz steps)."""
        if int(hz) == int(self.cfg.get("low_fc_offset_hz", 0)):
            return
        self.cfg["low_fc_offset_hz"] = int(hz)
        save_config(self.cfg, self.config_path)
        self.apo.force_rewrite()
        log.info("LOW crossover offset -> %+d Hz", hz)
        self._notify()

    def set_fader_curve(self, curve: str) -> None:
        if curve not in ApoWriter.FADER_CURVES or curve == self.cfg.get("fader_curve"):
            return
        self.cfg["fader_curve"] = curve
        save_config(self.cfg, self.config_path)
        if FADER in self.raw:  # re-map the fader's current position
            self.apo.set_target(FADER, self.apo.fader_to_db(self.raw[FADER]))
        log.info("Fader curve -> %s", curve)
        self._notify()

    def set_user_bypass(self, on: bool) -> None:
        if on == self.user_bypass:
            return
        self.user_bypass = on
        if on:
            self.apo.bypass("bypassed by user")
            log.info("EQ bypassed by user")
        elif self.state == "active":
            self.apo.resume()
            log.info("EQ re-engaged")
        self._notify()

    def learn(self, control: str) -> None:
        """Next control-change from the controller is assigned to `control`."""
        self.learning = control
        self._learn_deadline = time.monotonic() + LEARN_TIMEOUT_S
        log.info("MIDI learn: move the %s control now", control.upper())
        self._notify()

    def status_text(self) -> str:
        if self.apo.last_error:
            return "Error - cannot write APO file (see log)"
        if self.learning:
            return f"MIDI learn: move the {self.learning.upper()} control..."
        if self.state == "active" and self.user_bypass:
            return f"Bypassed (tray) - {self.midi.name}"
        return {
            "active": f"Active - {self.midi.name}, channel {self.cfg['midi_channel']}, {self.cfg['eq_mode']}",
            "yielded": "Yielded - DJ software running, EQ bypassed",
            "no_device": "Waiting for controller",
            "starting": "Starting",
        }.get(self.state, self.state)

    # -- internals ---------------------------------------------------------- #
    def _notify(self) -> None:
        if self.on_state:
            try:
                self.on_state(self.state)
            except Exception:  # noqa: BLE001
                pass

    def _set_state(self, new: str) -> None:
        if new != self.state:
            log.info("State: %s -> %s", self.state, new)
            self.state = new
            self._notify()

    def _on_raw(self, status: int, control: int, value: int) -> None:
        if not self.learning or (status & 0xF0) != 0xB0:
            return
        lsb = int(self.cfg["lsb_offset"])
        if lsb <= control < lsb + 32:
            return  # fine-resolution LSB of a 14-bit pair; wait for the MSB
        target = self.learning
        self.learning = None
        ch = (status & 0x0F) + 1
        if target == FILTER:
            # The filter knob has its own channel; don't touch the mixer strip.
            self.cfg["filter_cc"] = control
            self.cfg["filter_channel"] = ch
        else:
            if target == FADER:
                self.cfg["fader_cc"] = control
            else:
                self.cfg["cc"][target] = control
            self.cfg["midi_channel"] = ch
        self.midi.reconfigure()
        save_config(self.cfg, self.config_path)
        log.info("MIDI learn: %s = CC %d on channel %d", target.upper(), control, ch)
        self._notify()

    def _on_knob(self, control: str, value14: int) -> None:
        self.raw[control] = value14
        if self.state != "active" or self.user_bypass:
            return
        if control == FADER:
            self.apo.set_target(control, self.apo.fader_to_db(value14))
        elif control == FILTER:
            self.apo.set_target(control, self.apo.knob_to_position(value14))
        else:
            self.apo.set_target(control, self.apo.knob_to_db(value14))

    def _supervisor(self) -> None:
        proc_iv = float(self.cfg["process_poll_seconds"])
        dev_iv = float(self.cfg["device_poll_seconds"])
        next_proc = 0.0
        next_dev = 0.0
        next_save = 0.0
        dj_running = False
        prev_error = None

        while not self._stop.is_set():
            now = time.monotonic()

            if now >= next_proc:
                next_proc = now + proc_iv
                dj_running = self.procs.poll()

            if dj_running:
                if self.state != "yielded":
                    self.midi.close()
                    self.apo.bypass()
                    self._set_state("yielded")
            else:
                if self.state in ("yielded", "starting"):
                    if not self.user_bypass:
                        self.apo.resume()
                    self._set_state("no_device")
                    next_dev = 0.0

                if now >= next_dev:
                    next_dev = now + dev_iv
                    self.apo.ensure_include()
                    if self.state == "no_device":
                        if self.midi.open():
                            self._set_state("active")
                    elif self.state == "active" and not self.midi.still_present():
                        log.warning("Controller disappeared")
                        self.midi.close()
                        self._set_state("no_device")

            if self.learning and now > self._learn_deadline:
                log.info("MIDI learn timed out")
                self.learning = None
                self._notify()

            if self.apo.last_error != prev_error:
                prev_error = self.apo.last_error
                self._notify()

            if now >= next_save and self.apo.dirty_state:
                next_save = now + 1.0
                save_state(self.apo.snapshot_targets())

            self._stop.wait(0.1)

    def _writer(self) -> None:
        period = 1.0 / float(self.cfg["write_rate_hz"])
        while not self._stop.is_set():
            self.apo.tick()
            self._stop.wait(period)


# --------------------------------------------------------------------------- #
# Tray UI
# --------------------------------------------------------------------------- #
def run_tray(bridge: Bridge) -> None:
    import pystray
    from PIL import Image, ImageDraw

    colours = {
        "active": (46, 204, 113, 255),
        "bypassed": (52, 152, 219, 255),
        "learning": (155, 89, 182, 255),
        "yielded": (241, 196, 15, 255),
        "no_device": (127, 140, 141, 255),
        "starting": (127, 140, 141, 255),
        "error": (231, 76, 60, 255),
    }

    def make_icon(state: str):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=colours.get(state, colours["error"]))
        d.rectangle((26, 20, 38, 44), fill=(255, 255, 255, 220))  # fader cap
        return img

    def icon_state() -> str:
        if bridge.apo.last_error:
            return "error"
        if bridge.learning:
            return "learning"
        if bridge.state == "active" and bridge.user_bypass:
            return "bypassed"
        return bridge.state

    icon = pystray.Icon(APP_NAME, make_icon("starting"), "DDJ Bridge")

    def refresh(_state=None):
        icon.icon = make_icon(icon_state())
        icon.title = f"DDJ Bridge - {bridge.status_text()}"
        try:
            icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    # pystray inspects callables' arity: an action takes (icon, item) and a
    # checked-predicate takes (item). Default-argument captures would count
    # as extra parameters and be rejected, so bind values with closures.
    def act(fn, *args):
        return lambda _icon, _item: fn(*args)

    def chk(fn, *args):
        return lambda _item: fn(*args)

    # -- dynamic submenus --------------------------------------------------- #
    def channel_items():
        n = bridge.channels_available()
        for ch in range(1, n + 1):
            yield pystray.MenuItem(
                f"Channel {ch}", act(bridge.set_channel, ch),
                checked=chk(lambda c: int(bridge.cfg["midi_channel"]) == c, ch),
                radio=True)

    def controller_items():
        cands = bridge.midi.candidates()
        if not cands:
            yield pystray.MenuItem("(no controller found)", None, enabled=False)
            return
        chosen = bridge.midi.name or bridge.cfg.get("midi_port_name") or cands[0][1]
        for _i, name in cands:
            yield pystray.MenuItem(
                f"{name}  -  {profile_for(name)['name']}", act(bridge.set_port, name),
                checked=chk(lambda n: n == chosen, name),
                radio=True)

    def mode_items():
        labels = {"isolator": "ISOLATOR  (DJM-A9 curve, -inf..+6 dB)", "eq": "EQ  (DJM-A9 curve, -26..+6 dB)"}
        for key in bridge.cfg["eq_modes"]:
            yield pystray.MenuItem(
                labels.get(key, key), act(bridge.set_mode, key),
                checked=chk(lambda k: bridge.cfg["eq_mode"] == k, key),
                radio=True)

    def resonance_items():
        for label, q in (("None  (Q 0.7)", 0.707), ("Mild  (Q 1.0)", 1.0),
                         ("Medium  (Q 1.4)", 1.4), ("Strong  (Q 2.0)", 2.0)):
            yield pystray.MenuItem(
                label, act(bridge.set_filter_q, q),
                checked=chk(lambda v: abs(float(bridge.cfg["filter_q"]) - v) < 0.05, q),
                radio=True)

    def fader_curve_items():
        for key, label in (("concave", "Concave  (rises near the top)"),
                           ("linear", "Linear"),
                           ("early_ramp", "Early ramp  (rises near the bottom)")):
            yield pystray.MenuItem(
                label, act(bridge.set_fader_curve, key),
                checked=chk(lambda k: bridge.cfg.get("fader_curve", "linear") == k, key),
                radio=True)

    def headphone_items():
        for hz in (-15, -10, -5, 0, 5, 10, 15):
            label = "Normal" if hz == 0 else f"{hz:+d} Hz"
            yield pystray.MenuItem(
                label, act(bridge.set_low_offset, hz),
                checked=chk(lambda v: int(bridge.cfg.get("low_fc_offset_hz", 0)) == v, hz),
                radio=True)

    def learn_items():
        for ctl, label in (("hi", "HI knob"), ("mid", "MID knob"), ("low", "LOW knob"),
                           (FADER, "Fader"), (FILTER, "Filter / CFX knob")):
            yield pystray.MenuItem(label, act(bridge.learn, ctl))

    def quit_(icon_, _item):
        icon_.stop()  # main() stops the bridge once the tray loop returns

    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda _i: bridge.status_text(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Controller", pystray.Menu(controller_items)),
        pystray.MenuItem("Mixer channel", pystray.Menu(channel_items)),
        pystray.MenuItem("EQ mode", pystray.Menu(mode_items)),
        pystray.MenuItem("Filter resonance", pystray.Menu(resonance_items)),
        pystray.MenuItem("Fader curve", pystray.Menu(fader_curve_items)),
        pystray.MenuItem("Headphone mode (LOW crossover)", pystray.Menu(headphone_items)),
        pystray.MenuItem("Bypass EQ",
                         act(lambda: bridge.set_user_bypass(not bridge.user_bypass)),
                         checked=chk(lambda: bridge.user_bypass)),
        pystray.MenuItem("MIDI learn", pystray.Menu(learn_items)),
        pystray.MenuItem("Reset EQ to flat", act(bridge.reset_flat)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open config folder", act(os.startfile, APP_DIR)),
        pystray.MenuItem("Open log", act(os.startfile, LOG_PATH)),
        pystray.MenuItem(f"About  (v{APP_VERSION})", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_),
    )
    bridge.on_state = refresh

    def setup(icon_):
        icon_.visible = True
        refresh()

    # The bridge is already running (started in main) - the tray is only a
    # status display, so a tray failure must never take the bridge down.
    icon.run(setup=setup)


def _say(msg: str) -> None:
    if sys.stdout is not None:
        print(msg, flush=True)


def run_console(bridge: Bridge) -> None:
    bridge.on_state = lambda s: _say(f"[{time.strftime('%H:%M:%S')}] {bridge.status_text()}")
    _say("Running. Ctrl+C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()


def run_monitor(cfg: dict) -> None:
    probe = MidiInput(cfg, lambda *_: None)
    found = probe.find()
    if not found:
        print(f"No MIDI input matching '{cfg['midi_port_match']}'. Ports: {probe.list_names()}")
        return
    idx, name = found

    def show(status: int, d1: int, d2: int) -> None:
        ch = (status & 0x0F) + 1
        kind = status & 0xF0
        if kind == 0xB0:
            print(f"ch {ch:2d}  CC {d1:3d} (0x{d1:02X})  value {d2:3d}", flush=True)
        elif kind in (0x90, 0x80):
            print(f"ch {ch:2d}  note {'on ' if kind == 0x90 else 'off'} {d1:3d}  vel {d2:3d}", flush=True)
        else:
            print(f"status 0x{status:02X}  {d1:3d}  {d2:3d}", flush=True)

    print(f"Monitoring '{name}' ({profile_for(name)['name']}) - move a knob. Ctrl+C to stop.")
    port = winmidi.MidiIn(idx, show)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        port.close()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
_mutex_handle = None


def single_instance() -> bool:
    global _mutex_handle
    if os.name != "nt":
        return True
    k32 = ctypes.windll.kernel32
    _mutex_handle = k32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
    return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def setup_logging(console: bool) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DDJ -> Equalizer APO bridge")
    ap.add_argument("--console", action="store_true", help="run without the tray icon")
    ap.add_argument("--monitor", action="store_true", help="print MIDI messages from the controller")
    ap.add_argument("--list-ports", action="store_true", help="list MIDI input ports")
    ap.add_argument("--flat", action="store_true", help="write a flat APO include file and exit")
    ap.add_argument("--config", type=Path, default=CONFIG_PATH, help="config.json path")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    args = ap.parse_args(argv)
    try:
        BOOT_LOG.write_text("")  # one boot per file
    except OSError:
        pass
    _crumb(f"main v{APP_VERSION} argv={sys.argv[1:]} frozen={getattr(sys, 'frozen', False)} "
           f"cwd={os.getcwd()} LOCALAPPDATA={os.environ.get('LOCALAPPDATA')} "
           f"stdout={'None' if sys.stdout is None else 'ok'}")

    interactive = args.console or args.monitor or args.list_ports or args.flat
    setup_logging(console=interactive)
    _crumb("logging ready")
    cfg = load_config(args.config)
    _crumb("config loaded")

    if args.list_ports:
        for n in MidiInput.list_names():
            print(f"{n}   [{profile_for(n)['name']}]" if re.search(cfg["midi_port_match"], n, re.I) else n)
        return 0
    if args.monitor:
        run_monitor(cfg)
        return 0
    if args.flat:
        w = ApoWriter(cfg)
        w.write_flat_now()
        print("Error: " + w.last_error if w.last_error else f"Wrote flat config to {w.path}")
        return 1 if w.last_error else 0

    if not single_instance():
        log.warning("Another instance is already running; exiting")
        _crumb("another instance holds the mutex; exiting")
        return 0
    _crumb("single instance ok")

    bridge = Bridge(cfg, args.config)
    # Start the audio/MIDI work first; the tray icon is optional cosmetics.
    bridge.start()
    _crumb("bridge started")
    if args.console:
        run_console(bridge)
    else:
        try:
            _crumb("starting tray")
            run_tray(bridge)
            _crumb("tray loop returned")
        except Exception as exc:  # noqa: BLE001
            log.warning("Tray icon unavailable (%s); running headless", exc)
            _crumb("tray failed: " + repr(exc))
            run_console(bridge)
        else:
            bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
