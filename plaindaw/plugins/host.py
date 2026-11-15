"""Uniform offline plugin interface over CLAP (own host) and VST3/AU (pedalboard).

render_plugin(job) is the single entry point used both in-process and inside the
sandbox worker. A job is a plain dict so it can be pickled to a child process.

job = {
  "format": "clap"|"vst3", "path": str, "plugin_id": str|None,
  "state": bytes|None, "params": {name: value}, "sample_rate": int, "block": int,
  "n_frames": int, "audio": ndarray (ch,n) | None,
  "notes": [(start_frame, end_frame, key, velocity_0_127)],   # instruments
  "param_points": [(frame, name, value)],                      # automation, sorted
}
returns {"audio": ndarray (2, n), "state": bytes|None, "param_names": [...]}
"""
from __future__ import annotations

import numpy as np
from pathlib import Path


def _clap(job: dict) -> dict:
    from .clap import ClapPlugin
    pl = ClapPlugin(job["path"], job.get("plugin_id"))
    try:
        if job.get("state"):
            pl.set_state(job["state"])
        if job.get("params"):
            pl.set_params(job["params"])
        pl.activate(job["sample_rate"], job["block"])
        events = []
        for s, e, k, v in job.get("notes") or []:
            events.append((int(s), 0, int(k), 0, max(0.0, min(1.0, v / 127.0))))
            events.append((int(e), 1, int(k), 0, 0.0))
        for f, name, val in job.get("param_points") or []:
            events.append((int(f), 2, pl.find_param(name).id, 0, float(val)))
        audio = job.get("audio")
        if audio is not None and pl.in_channels == 0:
            audio = None
        out = pl.process(audio, job["n_frames"], events)
        return {"audio": out.astype(np.float64), "state": pl.get_state(),
                "param_names": [p.name for p in pl.params]}
    finally:
        pl.close()


JUCE_B64 = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"


def juce_b64(data: bytes) -> str:
    """JUCE MemoryBlock::toBase64Encoding (size + '.' + 6-bit little-endian groups)."""
    acc = int.from_bytes(data, "little")
    n = (len(data) * 8 + 5) // 6
    return f"{len(data)}." + "".join(JUCE_B64[(acc >> (6 * i)) & 63] for i in range(n))


def juce_vst3_state(component: bytes) -> bytes:
    """Wrap a plugin's raw component state the way JUCE's VST3 host (pedalboard) stores it."""
    import struct
    xml = ('<?xml version="1.0" encoding="UTF-8"?> <VST3PluginState><IComponent>' + juce_b64(component)
           + "</IComponent></VST3PluginState>").encode() + b"\0"
    return b"VC2!" + struct.pack("<I", len(xml) - 1) + xml


def vst3_binary(path: str) -> str:
    """On Windows pedalboard can fail to scan a .vst3 bundle folder; load the DLL inside it instead."""
    import sys
    p = Path(path)
    if sys.platform == "win32" and p.is_dir():
        for arch in ("x86_64-win", "x64-win", "x86-win"):
            dll = p / "Contents" / arch / p.name
            if dll.is_file():
                return str(dll)
    return path


def _vst3(job: dict) -> dict:
    import pedalboard
    pl = pedalboard.load_plugin(vst3_binary(job["path"]))
    if job.get("state"):
        st = bytes(job["state"])
        if st[:4] == b"VST3":      # a .vstpreset file (from the sound library)
            pl.preset_data = st
        elif st[:4] == b"VC2!":    # JUCE VST3 state as saved by pedalboard
            pl.raw_state = st
        else:                      # a bare component state (e.g. a kit Surge XT patch shared with CLAP)
            pl.raw_state = juce_vst3_state(st)
    warm = bool(job.get("state"))
    for k, v in (job.get("params") or {}).items():
        setattr(pl, k, v)
    sr, n, B = job["sample_rate"], job["n_frames"], job["block"]
    points = list(job.get("param_points") or [])
    # chunk boundaries at automation points (quantised to block)
    chunk = max(B, 512)
    out = np.zeros((2, n), dtype=np.float64)
    pi = 0
    if pl.is_instrument:
        notes = sorted(job.get("notes") or [])
        msgs_all = []
        for s, e, k, v in notes:
            msgs_all.append((int(s), bytes([0x90, int(k), max(1, min(127, int(v)))])))
            msgs_all.append((int(e), bytes([0x80, int(k), 0])))
        msgs_all.sort(key=lambda m: (m[0], 0 if m[1][0] == 0x80 else 1))
        mi = 0
        if warm:   # some plugins (Surge XT) apply a loaded state on the next audio block
            pl([], duration=max(B, 512) / sr, sample_rate=sr, num_channels=2, buffer_size=B, reset=True)
        for start in range(0, n, chunk):
            m = min(chunk, n - start)
            while pi < len(points) and points[pi][0] < start + m:
                setattr(pl, points[pi][1], points[pi][2]); pi += 1
            msgs = []
            while mi < len(msgs_all) and msgs_all[mi][0] < start + m:
                msgs.append((msgs_all[mi][1], (msgs_all[mi][0] - start) / sr)); mi += 1
            a = pl(msgs, duration=m / sr, sample_rate=sr, num_channels=2, buffer_size=B, reset=(start == 0 and not warm))
            a = np.asarray(a, dtype=np.float64)
            out[:, start:start + min(m, a.shape[1])] = a[:2, :m]
    else:
        audio = job.get("audio")
        if audio is None:
            audio = np.zeros((2, n))
        audio = np.asarray(audio, dtype=np.float32)
        if audio.shape[0] == 1:
            audio = np.vstack([audio, audio])
        for start in range(0, n, chunk):
            m = min(chunk, n - start)
            while pi < len(points) and points[pi][0] < start + m:
                setattr(pl, points[pi][1], points[pi][2]); pi += 1
            a = pl.process(audio[:, start:start + m], sr, buffer_size=B, reset=(start == 0))
            a = np.asarray(a, dtype=np.float64)
            if a.shape[0] == 1:
                a = np.vstack([a, a])
            out[:, start:start + m] = a[:2, :m]
    state = None
    try:
        state = bytes(pl.raw_state)
    except Exception:
        pass
    return {"audio": out, "state": state, "param_names": list(pl.parameters.keys())}


def render_plugin(job: dict) -> dict:
    if job.get("kind") == "describe":
        from .scan import resolve
        fmt, path, pid = resolve(job["ref"], job.get("path"))
        return {"plugin": job["ref"], "path": path, **describe(fmt, path, pid)}
    if job["format"] == "clap":
        return _clap(job)
    if job["format"] == "vst3":
        return _vst3(job)
    raise ValueError(f"unknown plugin format {job['format']}")


def _plain(v):
    """pedalboard returns float/bool/str *subclasses* (weak wrappers) that can't be pickled to the sandbox."""
    if v is None:
        return None
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, (int, float)):
        return float(v)
    return str(v)


def describe(fmt: str, path: str, plugin_id: str | None = None) -> dict:
    """Parameter list for a plugin (name, min, max, default, current)."""
    if fmt == "clap":
        from .clap import ClapPlugin
        pl = ClapPlugin(path, plugin_id)
        try:
            return {"instrument": pl.is_instrument,
                    "params": [{"name": p.name, "id": p.id, "min": p.min, "max": p.max, "default": p.default,
                                "value": pl.param_value(p)} for p in pl.params]}
        finally:
            pl.close()
    import pedalboard
    pl = pedalboard.load_plugin(vst3_binary(path))
    params = []
    for k, p in pl.parameters.items():
        item = {"name": k, "label": getattr(p, "units", "") or ""}
        try:
            item["value"] = _plain(getattr(pl, k))
        except Exception:
            pass
        try:
            rng = getattr(p, "range", None)
            if rng:
                item["min"], item["max"] = _plain(rng[0]), _plain(rng[1])
            vals = getattr(p, "valid_values", None)
            if vals and len(vals) <= 64 and isinstance(vals[0], str):
                item["choices"] = [str(v) for v in vals]
        except Exception:
            pass
        params.append(item)
    return {"instrument": bool(pl.is_instrument), "params": params}
