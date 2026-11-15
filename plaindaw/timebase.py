"""Musical time: positions, time signatures and the tempo map.

Internal unit is the quarter note ("qn", float). Positions in files are either a
number of quarter notes from the song start (0 = bar 1) or a string
"bar.beat[.sixteenth[.tick]]" (1-based bar, beat and sixteenth; tick 0-based,
240 per sixteenth), where "beat" is the time-signature denominator unit.
Time-signature changes must sit on bar starts.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

TICKS_PER_16TH = 240
_POS = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?\s*$")
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLATS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}


class TimeError(ValueError):
    pass


@dataclass
class Sig:
    bar: int      # 1-based bar where it starts
    num: int
    den: int
    qn_start: float = 0.0

    @property
    def qn_per_bar(self) -> float:
        return self.num * 4.0 / self.den

    @property
    def qn_per_beat(self) -> float:
        return 4.0 / self.den


class TimeMap:
    def __init__(self, transport: dict):
        sigs = transport.get("timeSig") or [{"at": "1.1.1", "num": 4, "den": 4}]
        raw = []
        for s in sigs:
            at = s.get("at", "1.1.1")
            m = _POS.match(str(at)) if isinstance(at, str) else None
            if not m or (m.group(2) not in (None, "1")) or (m.group(3) not in (None, "1")):
                raise TimeError(f"time signature change must be at a bar start: {at!r}")
            raw.append((int(m.group(1)), int(s["num"]), int(s["den"])))
        raw.sort()
        if raw[0][0] != 1:
            raw.insert(0, (1, 4, 4))
        self.sigs: list[Sig] = []
        qn = 0.0
        for i, (bar, num, den) in enumerate(raw):
            if self.sigs:
                prev = self.sigs[-1]
                qn = prev.qn_start + (bar - prev.bar) * prev.qn_per_bar
            self.sigs.append(Sig(bar, num, den, qn))
        tempos = transport.get("tempo") or [{"at": "1.1.1", "bpm": 120}]
        pts = sorted((self.qn(t.get("at", "1.1.1")), float(t["bpm"])) for t in tempos)
        if pts[0][0] > 0:
            pts.insert(0, (0.0, pts[0][1]))
        for _, bpm in pts:
            if not (10 <= bpm <= 999):
                raise TimeError(f"tempo out of range: {bpm}")
        self.tempo_qn = [p[0] for p in pts]
        self.tempo_bpm = [p[1] for p in pts]
        self.tempo_sec = [0.0]
        for i in range(1, len(pts)):
            self.tempo_sec.append(self.tempo_sec[-1] + (pts[i][0] - pts[i - 1][0]) * 60.0 / pts[i - 1][1])

    # ---- positions ----
    def _sig_for_bar(self, bar: int) -> Sig:
        i = bisect.bisect_right([s.bar for s in self.sigs], bar) - 1
        return self.sigs[max(0, i)]

    def _sig_for_qn(self, qn: float) -> Sig:
        i = bisect.bisect_right([s.qn_start for s in self.sigs], qn + 1e-9) - 1
        return self.sigs[max(0, i)]

    def qn(self, pos) -> float:
        """Position (string or number of quarter notes) -> quarter notes."""
        if isinstance(pos, bool):
            raise TimeError(f"bad position {pos!r}")
        if isinstance(pos, (int, float)):
            if pos < 0:
                raise TimeError(f"negative position {pos}")
            return float(pos)
        m = _POS.match(str(pos))
        if not m:
            raise TimeError(f"bad position {pos!r} (use 'bar.beat.16th' like '5.1.1' or quarter notes)")
        bar = int(m.group(1)); beat = int(m.group(2) or 1); six = int(m.group(3) or 1); tick = int(m.group(4) or 0)
        if bar < 1 or beat < 1 or six < 1 or tick < 0:
            raise TimeError(f"bad position {pos!r}: bar/beat/16th are 1-based")
        s = self._sig_for_bar(bar)
        if beat > s.num:
            raise TimeError(f"beat {beat} out of range in {s.num}/{s.den} at {pos!r}")
        return (s.qn_start + (bar - s.bar) * s.qn_per_bar + (beat - 1) * s.qn_per_beat
                + (six - 1) * 0.25 + tick * 0.25 / TICKS_PER_16TH)

    def pos(self, qn: float) -> str:
        """Quarter notes -> 'bar.beat.16th[.tick]'."""
        s = self._sig_for_qn(qn)
        rel = qn - s.qn_start
        bar = s.bar + int(rel // s.qn_per_bar + 1e-9)
        rem = rel - (bar - s.bar) * s.qn_per_bar
        beat = int(rem // s.qn_per_beat + 1e-9)
        rem -= beat * s.qn_per_beat
        six = int(rem // 0.25 + 1e-9)
        tick = int(round((rem - six * 0.25) / 0.25 * TICKS_PER_16TH))
        if tick >= TICKS_PER_16TH:
            tick, six = 0, six + 1
        out = f"{bar}.{beat + 1}.{six + 1}"
        return out + (f".{tick}" if tick else "")

    def bars_to_qn(self, bars: float, at_qn: float = 0.0) -> float:
        return bars * self._sig_for_qn(at_qn).qn_per_bar

    # ---- tempo ----
    def bpm_at(self, qn: float) -> float:
        i = bisect.bisect_right(self.tempo_qn, qn + 1e-12) - 1
        return self.tempo_bpm[max(0, i)]

    def sec(self, qn: float) -> float:
        i = max(0, bisect.bisect_right(self.tempo_qn, qn + 1e-12) - 1)
        return self.tempo_sec[i] + (qn - self.tempo_qn[i]) * 60.0 / self.tempo_bpm[i]

    def qn_at_sec(self, sec: float) -> float:
        i = max(0, bisect.bisect_right(self.tempo_sec, sec + 1e-12) - 1)
        return self.tempo_qn[i] + (sec - self.tempo_sec[i]) * self.tempo_bpm[i] / 60.0

    def to_sec(self, pos) -> float:
        return self.sec(self.qn(pos))


def note_number(key) -> int:
    """60, '60', 'C4', 'C#3', 'Eb2' -> MIDI key (C4 = 60)."""
    if isinstance(key, bool):
        raise TimeError(f"bad note {key!r}")
    if isinstance(key, (int, float)):
        k = int(key)
    elif isinstance(key, str) and key.strip().lstrip("-").isdigit():
        k = int(key)
    else:
        m = re.match(r"^\s*([A-Ga-g])([#b]?)(-?\d+)\s*$", str(key))
        if not m:
            raise TimeError(f"bad note name {key!r}")
        name = m.group(1).upper() + m.group(2)
        name = _FLATS.get(name, name)
        if name == "E#": name = "F"
        if name == "B#": name = "C"
        if name == "Cb": name = "B"
        if name == "Fb": name = "E"
        k = NOTE_NAMES.index(name) + (int(m.group(3)) + 1) * 12
    if not 0 <= k <= 127:
        raise TimeError(f"note out of range: {key!r}")
    return k


def note_name(k: int) -> str:
    return f"{NOTE_NAMES[k % 12]}{k // 12 - 1}"
