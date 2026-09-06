"""
Minimal Windows MIDI input via WinMM (ctypes). No compiled dependencies.

    names = list_inputs()
    port = MidiIn(index, callback)   # callback(status, data1, data2)
    port.close()
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_winmm = ctypes.windll.winmm

CALLBACK_FUNCTION = 0x00030000
MIM_DATA = 0x3C3
MMSYSERR_NOERROR = 0
MMSYSERR_ALLOCATED = 4  # device already in use

HMIDIIN = wintypes.HANDLE
DWORD_PTR = ctypes.c_size_t

_MidiInProc = ctypes.WINFUNCTYPE(None, HMIDIIN, wintypes.UINT, DWORD_PTR, DWORD_PTR, DWORD_PTR)


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwSupport", wintypes.DWORD),
    ]


_winmm.midiInGetNumDevs.restype = wintypes.UINT
_winmm.midiInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(MIDIINCAPSW), wintypes.UINT]
_winmm.midiInGetDevCapsW.restype = wintypes.UINT
_winmm.midiInOpen.argtypes = [ctypes.POINTER(HMIDIIN), wintypes.UINT, DWORD_PTR, DWORD_PTR, wintypes.DWORD]
_winmm.midiInOpen.restype = wintypes.UINT
for _fn in ("midiInStart", "midiInStop", "midiInReset", "midiInClose"):
    getattr(_winmm, _fn).argtypes = [HMIDIIN]
    getattr(_winmm, _fn).restype = wintypes.UINT
_winmm.midiInGetErrorTextW.argtypes = [wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
_winmm.midiInGetErrorTextW.restype = wintypes.UINT


class MidiError(OSError):
    def __init__(self, code: int, what: str):
        buf = ctypes.create_unicode_buffer(256)
        _winmm.midiInGetErrorTextW(code, buf, 256)
        super().__init__(f"{what}: {buf.value or 'unknown error'} (MMRESULT {code})")
        self.code = code

    @property
    def in_use(self) -> bool:
        return self.code == MMSYSERR_ALLOCATED


def list_inputs() -> list[str]:
    """MIDI input device names, index == device id."""
    names = []
    caps = MIDIINCAPSW()
    for i in range(_winmm.midiInGetNumDevs()):
        if _winmm.midiInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == MMSYSERR_NOERROR:
            names.append(caps.szPname)
        else:
            names.append(f"<device {i}>")
    return names


class MidiIn:
    """Open a MIDI input by device index; callback(status, data1, data2) on a WinMM thread."""

    def __init__(self, device_id: int, callback):
        self._callback = callback
        self._handle = HMIDIIN()
        # Keep the ctypes thunk referenced for the life of the port.
        self._proc = _MidiInProc(self._on_message)
        rc = _winmm.midiInOpen(ctypes.byref(self._handle), device_id,
                               ctypes.cast(self._proc, ctypes.c_void_p).value, 0, CALLBACK_FUNCTION)
        if rc != MMSYSERR_NOERROR:
            raise MidiError(rc, "midiInOpen")
        rc = _winmm.midiInStart(self._handle)
        if rc != MMSYSERR_NOERROR:
            _winmm.midiInClose(self._handle)
            raise MidiError(rc, "midiInStart")

    def _on_message(self, _h, msg, _inst, param1, _param2):
        if msg != MIM_DATA:
            return
        try:
            self._callback(param1 & 0xFF, (param1 >> 8) & 0x7F, (param1 >> 16) & 0x7F)
        except Exception:  # noqa: BLE001 - never let an exception escape a WinMM thread
            pass

    def close(self) -> None:
        if self._handle:
            _winmm.midiInStop(self._handle)
            _winmm.midiInReset(self._handle)
            _winmm.midiInClose(self._handle)
            self._handle = HMIDIIN()
