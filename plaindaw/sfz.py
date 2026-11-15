"""SFZ instruments: `sfz:<kit set | path.sfz>` as a track instrument.

Engines: sfizz (the kit's sfizz_render, a headless SFZ renderer from the sfizz project) when the
kit has it, else a small deterministic built-in player that covers the common opcodes
(sample, lokey/hikey/key, pitch_keycenter, lovel/hivel, tune, transpose, volume, amp_veltrack,
ampeg_attack/ampeg_release, offset, loop_mode one_shot). Set engine=builtin in the instrument
params to force the built-in player.
"""
from __future__ import annotations

import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .media import decode


class SfzError(RuntimeError):
    pass


def resolve(name: str, project_dir: Path) -> Path:
    n = str(name)
    p = Path(n)
    if p.suffix.lower() == ".sfz":
        q = p if p.is_absolute() else Path(project_dir) / p
        if q.exists():
            return q
        raise SfzError(f"SFZ file not found: {n}")
    from . import kit
    k = kit.sfz_path(n)
    if k is None:
        raise SfzError(f"SFZ set {n!r} is not installed (run `plaindaw kit install`)")
    return k


def _note(v: str) -> int:
    v = v.strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    m = re.fullmatch(r"([a-gA-G])([#b]?)(-?\d+)", v)
    if not m:
        raise SfzError(f"bad note {v!r}")
    pc = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}[m.group(1).lower()]
    pc += {"#": 1, "b": -1, "": 0}[m.group(2)]
    return 12 * (int(m.group(3)) + 1) + pc


def parse(path: Path) -> list[dict]:
    """Flatten <control>/<global>/<master>/<group>/<region> into a list of region dicts."""
    text = Path(path).read_text(errors="replace")
    text = re.sub(r"//[^\n]*", "", text)
    tokens = re.split(r"(<\w+>)", text)
    levels: dict = {"global": {}, "master": {}, "group": {}, "_target": None}
    control, regions, cur = {}, [], None
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        m = re.fullmatch(r"<(\w+)>", tok)
        if m:
            h = m.group(1)
            if cur is not None:
                regions.append(cur)
                cur = None
            if h == "region":
                cur = {**levels["global"], **levels["master"], **levels["group"]}
            elif h in ("global", "master", "group"):
                order = ["global", "master", "group"]
                for lower in order[order.index(h):]:
                    levels[lower] = {}
                levels["_target"] = h
            elif h == "control":
                levels["_target"] = "control"
            continue
        # opcodes: key=value pairs; sample= may contain spaces up to the next opcode
        for k, v in re.findall(r"(\w+)=(.*?)(?=\s+\w+=|$)", tok, flags=re.S):
            v = v.strip()
            if cur is not None:
                cur[k] = v
            elif levels.get("_target") == "control":
                control[k] = v
            elif levels.get("_target") in ("global", "master", "group"):
                levels[levels["_target"]][k] = v
    if cur is not None:
        regions.append(cur)
    base = Path(path).parent / control.get("default_path", "")
    out = []
    for r in regions:
        if "sample" not in r:
            continue
        key = _note(r["key"]) if "key" in r else None
        out.append({
            "sample": str((base / r["sample"].replace("\\", "/")).resolve()),
            "lokey": _note(r.get("lokey", str(key if key is not None else 0))),
            "hikey": _note(r.get("hikey", str(key if key is not None else 127))),
            "root": _note(r.get("pitch_keycenter", str(key if key is not None else 60))),
            "lovel": int(r.get("lovel", 1)), "hivel": int(r.get("hivel", 127)),
            "tune": float(r.get("tune", 0)) / 100 + float(r.get("transpose", 0)),
            "volume": float(r.get("volume", 0)), "veltrack": float(r.get("amp_veltrack", 100)) / 100,
            "attack": float(r.get("ampeg_attack", 0.0)), "release": float(r.get("ampeg_release", 0.05)),
            "offset": int(r.get("offset", 0)), "oneshot": r.get("loop_mode", "") == "one_shot",
        })
    if not out:
        raise SfzError(f"no playable regions in {path}")
    return out


def render_builtin(notes, n: int, sr: int, sfz_file: Path, params: dict | None = None) -> np.ndarray:
    regions = parse(sfz_file)
    gain = 10 ** (float((params or {}).get("gainDb", 0.0)) / 20)
    out = np.zeros((2, n))
    for s, e, k, v in notes:
        if s >= n:
            continue
        for r in regions:
            if not (r["lokey"] <= k <= r["hikey"] and r["lovel"] <= v <= r["hivel"]):
                continue
            x = decode(Path(r["sample"]), sr)[:, r["offset"]:]
            ratio = 2 ** ((k - r["root"] + r["tune"]) / 12.0)
            hold = e - s
            rel = int(r["release"] * sr)
            n_out = int(x.shape[1] / ratio) if r["oneshot"] else min(int(x.shape[1] / ratio), hold + rel)
            n_out = min(n_out, n - s)
            if n_out <= 0:
                continue
            pos = np.arange(n_out) * ratio
            xs = np.arange(x.shape[1])
            y = np.vstack([np.interp(pos, xs, x[0]), np.interp(pos, xs, x[1])])
            att = int(r["attack"] * sr)
            if att > 0:
                y[:, :min(att, n_out)] *= np.linspace(0, 1, att)[:min(att, n_out)]
            if not r["oneshot"] and n_out > hold:
                y[:, hold:] *= np.linspace(1, 0, n_out - hold)
            vel_gain = (1 - r["veltrack"]) + r["veltrack"] * (v / 127.0) ** 2
            out[:, s:s + n_out] += y * vel_gain * 10 ** (r["volume"] / 20) * gain
    return out


def _vlq(x: int) -> bytes:
    b = [x & 0x7F]
    x >>= 7
    while x:
        b.append(0x80 | (x & 0x7F))
        x >>= 7
    return bytes(reversed(b))


def write_midi(notes, sr: int, path: Path, ppq: int = 960) -> None:
    """Notes in samples -> SMF type 0 at 120 BPM (1 tick = 0.5/ppq s)."""
    ev = []
    for s, e, k, v in notes:
        ev.append((int(round(s / sr * 2 * ppq)), 1, k, int(max(1, min(127, round(v))))))
        ev.append((int(round(e / sr * 2 * ppq)), 0, k, 0))
    ev.sort(key=lambda x: (x[0], x[1]))
    trk, last = bytearray(), 0
    trk += _vlq(0) + bytes([0xFF, 0x51, 0x03]) + (500000).to_bytes(3, "big")
    for t, on, k, v in ev:
        trk += _vlq(t - last) + bytes([0x90 if on else 0x80, k, v])
        last = t
    trk += _vlq(0) + bytes([0xFF, 0x2F, 0x00])
    path.write_bytes(b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq) + b"MTrk" + struct.pack(">I", len(trk)) + bytes(trk))


def render_sfizz(exe: Path, notes, n: int, sr: int, sfz_file: Path, params: dict | None = None) -> np.ndarray:
    import soundfile as sf
    with tempfile.TemporaryDirectory(prefix="pd-sfizz-") as td:
        mid, wav = Path(td) / "in.mid", Path(td) / "out.wav"
        write_midi(notes, sr, mid)
        env = dict(os.environ)
        env.setdefault("DYLD_LIBRARY_PATH", str(exe.parent))
        r = subprocess.run([str(exe), "--sfz", str(sfz_file), "--midi", str(mid), "--wav", str(wav),
                            "-s", str(sr), "-b", "512"], capture_output=True, timeout=600, env=env)
        if r.returncode != 0 or not wav.exists():
            raise SfzError(f"sfizz_render failed: {r.stderr.decode(errors='replace')[-300:]}")
        y, ysr = sf.read(str(wav), always_2d=True, dtype="float64")
    y = y.T
    if ysr != sr:
        raise SfzError(f"sfizz rendered at {ysr} Hz, wanted {sr}")
    if y.shape[0] == 1:
        y = np.vstack([y, y])
    out = np.zeros((2, n))
    m = min(n, y.shape[1])
    out[:, :m] = y[:2, :m]
    return out * 10 ** (float((params or {}).get("gainDb", 0.0)) / 20)


def render_notes(name: str, notes, n: int, sr: int, project_dir: Path, params: dict | None = None,
                 warnings: list | None = None) -> np.ndarray:
    sfz_file = resolve(name, project_dir)
    engine = str((params or {}).get("engine", "auto")).lower()
    if engine in ("auto", "sfizz"):
        from . import kit
        exe = kit.sfizz_render()
        if exe is not None:
            try:
                return render_sfizz(exe, notes, n, sr, sfz_file, params)
            except (SfzError, OSError, subprocess.TimeoutExpired) as e:
                if warnings is not None:
                    warnings.append(f"sfizz failed, used the built-in SFZ player: {e}")
        elif engine == "sfizz" and warnings is not None:
            warnings.append("sfizz is not installed; used the built-in SFZ player")
    return render_builtin(notes, n, sr, sfz_file, params)
