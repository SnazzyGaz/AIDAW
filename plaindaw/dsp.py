"""Built-in instruments and effects (numpy/scipy, plus pedalboard's reverb/chorus).

All processing is offline on whole buffers shaped (2, n) float64, deterministic
for a given seed. Parameters are plain dicts; every effect documents its params
in PARAMS so the CLI/API can list them.
"""
from __future__ import annotations

import math
import os
from typing import Callable

import numpy as np
from scipy import signal

TWO_PI = 2 * math.pi


def db2lin(db):
    return np.power(10.0, np.asarray(db, dtype=np.float64) / 20.0)


def lin2db(x, floor=-200.0):
    x = np.asarray(x, dtype=np.float64)
    return np.maximum(20 * np.log10(np.maximum(x, 1e-12)), floor)


def pan_gains(p):
    p = np.clip(np.asarray(p, dtype=np.float64), -1, 1)
    th = (p + 1) * math.pi / 4
    return np.minimum(1.0, math.sqrt(2) * np.cos(th)), np.minimum(1.0, math.sqrt(2) * np.sin(th))


def mtof(k):
    return 440.0 * 2.0 ** ((np.asarray(k, dtype=np.float64) - 69) / 12.0)


# ---------------------------------------------------------------- filters
def biquad(kind: str, f0: float, sr: float, q: float = 0.7071, gain_db: float = 0.0):
    f0 = float(np.clip(f0, 10.0, sr * 0.49))
    A = 10 ** (gain_db / 40)
    w = TWO_PI * f0 / sr
    cw, sw = math.cos(w), math.sin(w)
    alpha = sw / (2 * max(q, 1e-3))
    if kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "bandpass":
        b = [alpha, 0, -alpha]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "peak":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]; a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif kind == "lowshelf":
        sq = 2 * math.sqrt(A) * alpha
        b = [A * ((A + 1) - (A - 1) * cw + sq), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sq)]
        a = [(A + 1) + (A - 1) * cw + sq, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sq]
    elif kind == "highshelf":
        sq = 2 * math.sqrt(A) * alpha
        b = [A * ((A + 1) + (A - 1) * cw + sq), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sq)]
        a = [(A + 1) - (A - 1) * cw + sq, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sq]
    else:
        raise ValueError(kind)
    b = np.array(b) / a[0]; a = np.array(a) / a[0]
    return np.concatenate([b, a])  # sos row


def sos_filter(sos_rows, x):
    if not sos_rows:
        return x
    sos = np.vstack(sos_rows)
    return signal.sosfilt(sos, x, axis=-1)


_NATIVE = None


def _native():
    global _NATIVE
    if _NATIVE is None:
        _NATIVE = False
        if os.environ.get("PLAINDAW_NO_NATIVE") != "1":
            try:
                import ctypes
                from .native_build import dsp_lib
                p = dsp_lib()
                if p:
                    lib = ctypes.CDLL(str(p))
                    D = ctypes.POINTER(ctypes.c_double)
                    lib.pd_tvbiquad.argtypes = [D, D, ctypes.c_int, ctypes.c_long, D, ctypes.c_int, ctypes.c_double,
                                                ctypes.c_double, ctypes.c_double, ctypes.c_int]
                    lib.pd_tvbiquad.restype = None
                    _NATIVE = lib
            except Exception:
                _NATIVE = False
    return _NATIVE


_KIND = {"lowpass": 0, "highpass": 1, "bandpass": 2, "peak": 3}


def timevarying(kind: str, x: np.ndarray, sr: float, freq: np.ndarray, q: float, block: int = 32,
                gain_db: float = 0.0) -> np.ndarray:
    lib = _native()
    if lib and kind in _KIND:
        import ctypes
        x2 = np.ascontiguousarray(x if x.ndim > 1 else x[None, :], dtype=np.float64)
        n = x2.shape[-1]
        f = np.asarray(freq, dtype=np.float64)
        f = np.ascontiguousarray(np.full(n, float(f)) if f.ndim == 0 else f)
        y = np.empty_like(x2)
        D = ctypes.POINTER(ctypes.c_double)
        lib.pd_tvbiquad(x2.ctypes.data_as(D), y.ctypes.data_as(D), x2.shape[0], n, f.ctypes.data_as(D),
                        _KIND[kind], float(q), float(sr), float(gain_db), int(block))
        return y if x.ndim > 1 else y[0]
    return _timevarying_py(kind, x, sr, freq, q, max(block, 128), gain_db)


def _timevarying_py(kind: str, x: np.ndarray, sr: float, freq: np.ndarray, q: float, block: int = 128,
                    gain_db: float = 0.0) -> np.ndarray:
    """Biquad with a per-sample cutoff curve, coefficients updated every `block` samples
    (all channels filtered together; state carried across blocks)."""
    n = x.shape[-1]
    out = np.empty_like(x)
    x2 = x if x.ndim > 1 else x[None, :]
    o2 = out if x.ndim > 1 else out[None, :]
    zi = np.zeros((1, x2.shape[0], 2))
    freq = np.asarray(freq, dtype=np.float64)
    if freq.ndim == 0:
        freq = np.full(n, float(freq))
    last_f, row = None, None
    for s in range(0, n, block):
        e = min(n, s + block)
        f = float(freq[min(s + block // 2, n - 1)])
        if row is None or abs(f - last_f) > 1e-6 * max(1.0, last_f):
            row = biquad(kind, f, sr, q, gain_db)[None, :]
            last_f = f
        o2[:, s:e], zi = signal.sosfilt(row, x2[:, s:e], axis=-1, zi=zi)
    return out


# ---------------------------------------------------------------- envelopes
def adsr(n_hold: int, sr: float, a: float, d: float, s: float, r: float) -> np.ndarray:
    """Envelope for a note held n_hold samples then released (total n_hold + r)."""
    A, D, R = max(1, int(a * sr)), max(1, int(d * sr)), max(1, int(r * sr))
    t = np.arange(n_hold, dtype=np.float64)
    env = np.where(t < A, t / A, s + (1 - s) * np.exp(-np.maximum(0, t - A) / (D / 4.0)))
    level = env[-1] if n_hold else 0.0
    rel = level * np.exp(-np.arange(R) / (R / 6.0)) * np.linspace(1, 0, R) ** 0.25
    return np.concatenate([env, rel])


def block_env(level: np.ndarray, attack_s: float, release_s: float, sr: float, hop: int = 32) -> np.ndarray:
    """Peak follower on `level` (mono, linear) computed per hop and interpolated."""
    n = level.shape[-1]
    nb = (n + hop - 1) // hop
    pad = np.zeros(nb * hop); pad[:n] = level
    blk = pad.reshape(nb, hop).max(axis=1)
    ca = math.exp(-hop / max(1, attack_s * sr))
    cr = math.exp(-hop / max(1, release_s * sr))
    env = np.empty(nb)
    e = 0.0
    for i, v in enumerate(blk):
        c = ca if v > e else cr
        e = c * e + (1 - c) * v
        env[i] = e
    return np.interp(np.arange(n), np.arange(nb) * hop + hop / 2, env)


# ---------------------------------------------------------------- oscillators
def _polyblep(t, dt):
    out = np.zeros_like(t)
    m = t < dt
    x = t[m] / dt[m]
    out[m] = 2 * x - x * x - 1
    m2 = t > 1 - dt
    x = (t[m2] - 1) / dt[m2]
    out[m2] = x * x + 2 * x + 1
    return out


def osc(wave: str, freq: np.ndarray, sr: float, phase0: float, rng: np.random.Generator) -> np.ndarray:
    dt = np.minimum(freq / sr, 0.49)
    ph = (phase0 + np.cumsum(dt) - dt[0]) % 1.0
    if wave == "sine":
        return np.sin(TWO_PI * ph)
    if wave == "saw":
        return 2 * ph - 1 - _polyblep(ph, dt)
    if wave == "square":
        sq = np.where(ph < 0.5, 1.0, -1.0)
        return sq + _polyblep(ph, dt) - _polyblep((ph + 0.5) % 1.0, dt)
    if wave == "triangle":
        return 2 * np.abs(2 * ph - 1) - 1
    if wave == "noise":
        return rng.uniform(-1, 1, size=freq.shape[0])
    raise ValueError(f"unknown wave {wave!r}")


# ---------------------------------------------------------------- instruments
SYNTH_DEFAULTS = {"wave": "saw", "voices": 1, "detune": 12.0, "octave": 0, "cutoff": 8000.0, "resonance": 0.2,
                  "envAmount": 0.0, "filterDecay": 0.3, "attack": 0.005, "decay": 0.2, "sustain": 0.7,
                  "release": 0.15, "gainDb": -6.0, "width": 0.8, "sub": 0.0, "glide": 0.0, "pitchEnv": 0.0,
                  "pitchDecay": 0.05, "mono": False}


def synth_note(p: dict, key: int, vel: float, n_hold: int, sr: float, rng: np.random.Generator,
               from_key: int | None = None) -> np.ndarray:
    """One note. from_key: the previous legato note in mono mode; the pitch slides from it to
    `key` over `glide` seconds (808-style slides) and the pitch-envelope punch is skipped."""
    voices = int(np.clip(p["voices"], 1, 9))
    env = adsr(n_hold, sr, p["attack"], p["decay"], p["sustain"], p["release"])
    n = env.shape[0]
    t = np.arange(n) / sr
    f0 = mtof(key + 12 * int(p["octave"]))
    pitch = 1.0
    if from_key is not None and from_key != key and p.get("glide", 0) > 0:
        ramp = np.clip(1.0 - t / max(1e-3, float(p["glide"])), 0.0, 1.0)
        pitch = 2.0 ** ((from_key - key) * ramp ** 2 / 12.0)
    elif p["pitchEnv"]:
        pitch = 2.0 ** (p["pitchEnv"] * np.exp(-t / max(1e-3, p["pitchDecay"])) / 12.0)
    out = np.zeros((2, n))
    for v in range(voices):
        spread = 0.0 if voices == 1 else (v / (voices - 1)) * 2 - 1
        cents = spread * p["detune"]
        f = np.full(n, f0 * 2 ** (cents / 1200.0)) * pitch
        s = osc(p["wave"], f, sr, rng.uniform(0, 1) if voices > 1 else 0.0, rng)
        pan = spread * p["width"]
        gl, gr = pan_gains(pan)
        out[0] += s * gl; out[1] += s * gr
    out /= math.sqrt(voices)
    if p["sub"]:
        s = osc("sine", np.full(n, f0 / 2), sr, 0.0, rng) * p["sub"]
        out += s
    cutoff = p["cutoff"]
    if p["envAmount"]:
        fenv = np.exp(-t / max(1e-3, p["filterDecay"]))
        cut = np.clip(cutoff * 2.0 ** (p["envAmount"] * fenv), 20, sr * 0.45)
    else:
        cut = None
    q = 0.707 + p["resonance"] * 8
    if cut is not None:
        out = timevarying("lowpass", out, sr, cut, q)
    elif cutoff < sr * 0.45:
        out = sos_filter([biquad("lowpass", cutoff, sr, q)], out)
    return out * env * (vel / 127.0) * db2lin(p["gainDb"])


def drum_hit(key: int, vel: float, sr: float, rng: np.random.Generator, p: dict) -> np.ndarray:
    """Synthesised GM-style kit: 36 kick, 37 rim, 38 snare, 39 clap, 42/44 closed hat,
    46 open hat, 41/45/48 toms, 49/57 crash, 51 ride."""
    tune = 2 ** (p.get("tune", 0.0) / 12)
    g = (vel / 127.0) * db2lin(p.get("gainDb", -3.0))
    def env(dur, curve=6.0):
        n = int(dur * sr); return np.exp(-np.arange(n) / (n / curve))
    if key in (35, 36):
        dec = p.get("kickDecay", 0.45); n = int(dec * sr); t = np.arange(n) / sr
        f = (p.get("kickPitch", 50.0) * tune) * (1 + 3.5 * np.exp(-t / 0.035))
        ph = np.cumsum(f / sr)
        body = np.sin(TWO_PI * ph) * np.exp(-t / (dec / 3))
        click = rng.uniform(-1, 1, n) * np.exp(-t / 0.003) * 0.35
        s = np.tanh((body + click) * 1.6) * 0.9
    elif key == 37:
        e = env(0.06, 8); s = np.sin(TWO_PI * 1700 * tune * np.arange(e.size) / sr) * e * 0.5
    elif key in (38, 40):
        e = env(p.get("snareDecay", 0.22), 5); t = np.arange(e.size) / sr
        tone = np.sin(TWO_PI * 185 * tune * t) * np.exp(-t / 0.04)
        noise = sos_filter([biquad("highpass", 1200, sr), biquad("lowpass", 9000, sr)], rng.uniform(-1, 1, e.size))
        s = (tone * 0.6 + noise * 0.8) * e
    elif key == 39:
        n = int(0.35 * sr); s = np.zeros(n); t = np.arange(n) / sr
        nz = sos_filter([biquad("bandpass", 1300 * tune, sr, 1.2)], rng.uniform(-1, 1, n)) * 3
        for k, off in enumerate((0.0, 0.011, 0.022)):
            o = int(off * sr); m = n - o
            s[o:] += nz[o:] * np.exp(-np.arange(m) / (0.008 * sr)) * (0.8 if k < 2 else 0)
        o = int(0.03 * sr); s[o:] += nz[o:] * np.exp(-np.arange(n - o) / (0.09 * sr))
        s *= 0.7
    elif key in (42, 44, 46):
        dur = p.get("hatDecay", 0.05) if key != 46 else p.get("openHatDecay", 0.35)
        e = env(dur, 5)
        s = sos_filter([biquad("highpass", 7000, sr, 0.9)], rng.uniform(-1, 1, e.size)) * e * 0.6
    elif key in (41, 43, 45, 47, 48, 50):
        base = {41: 80, 43: 95, 45: 110, 47: 130, 48: 150, 50: 175}[key] * tune
        e = env(0.35, 5); t = np.arange(e.size) / sr
        s = np.sin(TWO_PI * np.cumsum(base * (1 + 0.6 * np.exp(-t / 0.05)) / sr)) * e * 0.8
    elif key in (49, 52, 55, 57):
        e = env(p.get("crashDecay", 1.6), 5)
        s = sos_filter([biquad("highpass", 4000, sr, 0.7)], rng.uniform(-1, 1, e.size)) * e * 0.45
    elif key in (51, 53, 59):
        e = env(0.9, 5); t = np.arange(e.size) / sr
        s = (sos_filter([biquad("bandpass", 6000, sr, 2)], rng.uniform(-1, 1, e.size)) + 0.2 * np.sin(TWO_PI * 3800 * t)) * e * 0.4
    else:
        return np.zeros((2, 1))
    s = s * g
    return np.vstack([s, s])


def sampler_note(sample: np.ndarray, sr: float, key: int, vel: float, n_hold: int, p: dict) -> np.ndarray:
    """sample: (2, m) at sr. Pitched by linear interpolation relative to root."""
    ratio = 2 ** ((key - float(p.get("root", 60)) + p.get("tune", 0.0)) / 12.0)
    m = sample.shape[1]
    oneshot = p.get("mode", "oneshot") == "oneshot"
    rel = int(p.get("release", 0.05) * sr)
    n_out = int(m / ratio) if oneshot else min(int(m / ratio), n_hold + rel)
    if n_out <= 0:
        return np.zeros((2, 1))
    pos = np.arange(n_out) * ratio
    xs = np.arange(m)
    out = np.vstack([np.interp(pos, xs, sample[0]), np.interp(pos, xs, sample[1])])
    att = int(p.get("attack", 0.0) * sr)
    if att > 0:
        out[:, :min(att, n_out)] *= np.linspace(0, 1, att)[:min(att, n_out)]
    if not oneshot and n_out > n_hold:
        r = n_out - n_hold
        out[:, n_hold:] *= np.linspace(1, 0, r)
    return out * (vel / 127.0) * db2lin(p.get("gainDb", 0.0))


# ---------------------------------------------------------------- effects
class Ctx:
    """What an effect may need: sample rate, tempo, automation curves, sidechain."""

    def __init__(self, sr: float, bpm_at_sample: Callable[[np.ndarray], np.ndarray], n: int,
                 curves: dict | None = None, sidechain: np.ndarray | None = None, beat_at_sample=None,
                 seed: int = 1):
        self.sr, self.n = sr, n
        self.bpm_at_sample = bpm_at_sample
        self.beat_at_sample = beat_at_sample
        self.curves = curves or {}
        self.sidechain = sidechain
        self.seed = seed

    def curve(self, name: str, default: float) -> np.ndarray | None:
        return self.curves.get(name)


def _mix(dry, wet, mix):
    return dry * (1 - mix) + wet * mix


def fx_eq(x, p, ctx):
    rows = []
    sr = ctx.sr
    if p.get("lowCutHz") and "lowCutHz" not in ctx.curves:
        rows += [biquad("highpass", p["lowCutHz"], sr, 0.7071)] * (2 if p.get("steep", False) else 1)
    if p.get("highCutHz") and "highCutHz" not in ctx.curves:
        rows += [biquad("lowpass", p["highCutHz"], sr, 0.7071)]
    if p.get("lowShelfDb"):
        rows.append(biquad("lowshelf", p.get("lowShelfHz", 120), sr, 0.7071, p["lowShelfDb"]))
    if p.get("highShelfDb"):
        rows.append(biquad("highshelf", p.get("highShelfHz", 8000), sr, 0.7071, p["highShelfDb"]))
    for i in (1, 2, 3):
        if p.get(f"peak{i}Db"):
            rows.append(biquad("peak", p.get(f"peak{i}Hz", 1000), sr, p.get(f"peak{i}Q", 1.0), p[f"peak{i}Db"]))
    y = sos_filter(rows, x)
    if "lowCutHz" in ctx.curves:
        y = timevarying("highpass", y, sr, ctx.curves["lowCutHz"], 0.7071, 256)
    if "highCutHz" in ctx.curves:
        y = timevarying("lowpass", y, sr, ctx.curves["highCutHz"], 0.7071, 256)
    return y


def fx_filter(x, p, ctx):
    kind = p.get("type", "lowpass")
    q = 0.707 + p.get("resonance", 0.0) * 8
    cut = ctx.curves.get("cutoff")
    if cut is None:
        y = sos_filter([biquad(kind, p.get("cutoff", 1000.0), ctx.sr, q)], x)
    else:
        y = timevarying(kind, x, ctx.sr, cut, q, 128)
    mix = ctx.curves.get("mix", p.get("mix", 1.0))
    return _mix(x, y, mix)


def fx_compressor(x, p, ctx):
    thr, ratio = p.get("thresholdDb", -18.0), max(1.0, p.get("ratio", 4.0))
    knee = p.get("kneeDb", 6.0)
    det = ctx.sidechain if (p.get("_sidechain") and ctx.sidechain is not None) else x
    level = np.max(np.abs(det), axis=0)
    env = block_env(level, p.get("attack", 0.005), p.get("release", 0.12), ctx.sr)
    ldb = lin2db(env)
    over = ldb - thr
    gr = np.where(over <= -knee / 2, 0.0,
                  np.where(over >= knee / 2, over * (1 - 1 / ratio),
                           (1 - 1 / ratio) * (over + knee / 2) ** 2 / (2 * max(knee, 1e-6))))
    g = db2lin(-gr + p.get("makeupDb", 0.0))
    y = x * g
    return _mix(x, y, ctx.curves.get("mix", p.get("mix", 1.0)))


def true_peak_env(x, factor=4):
    up = signal.resample_poly(x, factor, 1, axis=-1)
    m = np.max(np.abs(up), axis=0)
    n = x.shape[-1]
    m = m[: n * factor].reshape(n, factor).max(axis=1) if m.size >= n * factor else np.pad(m, (0, n * factor - m.size)).reshape(n, factor).max(axis=1)
    return m


def fx_limiter(x, p, ctx):
    from scipy.ndimage import minimum_filter1d, uniform_filter1d
    ceil = db2lin(p.get("ceilingDb", -1.0))
    x = x * db2lin(p.get("gainDb", 0.0))
    look = max(8, int(p.get("lookaheadMs", 1.5) * ctx.sr / 1000))
    peak = np.maximum(true_peak_env(x), np.max(np.abs(x), axis=0))
    req = np.minimum(1.0, ceil / np.maximum(peak, 1e-12))
    # forward-looking minimum over 2*look then causal box smoothing over look
    gmin = minimum_filter1d(req, size=2 * look, origin=-(look - 1) if look > 1 else 0, mode="nearest")
    gs = uniform_filter1d(gmin, size=look, origin=(look - 1) // 2, mode="nearest")
    gs = np.minimum(gs, req)
    # release: let gain recover smoothly (block-level one-pole upward)
    rel = p.get("release", 0.08)
    hop = 32
    nb = (gs.size + hop - 1) // hop
    blk = np.pad(gs, (0, nb * hop - gs.size), constant_values=1.0).reshape(nb, hop).min(axis=1)
    cr = math.exp(-hop / max(1, rel * ctx.sr))
    e = 1.0
    sm = np.empty(nb)
    for i, v in enumerate(blk):
        e = v if v < e else cr * e + (1 - cr) * v
        sm[i] = e
    rel_curve = np.repeat(sm, hop)[: gs.size]
    g = np.minimum(gs, rel_curve)
    y = x * g
    return np.clip(y, -ceil, ceil)


def fx_gate(x, p, ctx):
    level = np.max(np.abs(x), axis=0)
    env = block_env(level, 0.001, p.get("release", 0.05), ctx.sr)
    g = (lin2db(env) > p.get("thresholdDb", -50.0)).astype(float)
    g = block_env(g, p.get("attack", 0.001), p.get("release", 0.05), ctx.sr)
    return x * np.maximum(g, db2lin(p.get("floorDb", -80.0)))


def fx_reverb(x, p, ctx):
    import pedalboard
    pre = int(p.get("predelayMs", 0.0) * ctx.sr / 1000)
    rv = pedalboard.Reverb(room_size=float(np.clip(p.get("size", 0.7), 0, 1)),
                           damping=float(np.clip(p.get("damping", 0.5), 0, 1)),
                           wet_level=1.0, dry_level=0.0, width=float(np.clip(p.get("width", 1.0), 0, 1)))
    src = np.pad(x, ((0, 0), (pre, 0)))[:, : x.shape[1]] if pre else x
    wet = rv(src.astype(np.float32), ctx.sr).astype(np.float64)
    if p.get("lowCutHz"):
        wet = sos_filter([biquad("highpass", p["lowCutHz"], ctx.sr)], wet)
    mix = ctx.curves.get("mix", p.get("mix", 0.3))
    return x * (1 - mix) + wet * mix


def fx_delay(x, p, ctx):
    n = x.shape[1]
    if "timeBeats" in p:
        bpm = float(np.median(ctx.bpm_at_sample(np.array([0, n // 2, max(0, n - 1)]))))
        d = int(p["timeBeats"] * 60.0 / bpm * ctx.sr)
    else:
        d = int(p.get("timeMs", 375.0) * ctx.sr / 1000)
    d = max(1, d)
    fb = float(np.clip(p.get("feedback", 0.35), 0, 0.95))
    ping = p.get("pingpong", False)
    wet = np.zeros_like(x)
    src = x.copy()
    if p.get("lowCutHz"):
        src = sos_filter([biquad("highpass", p["lowCutHz"], ctx.sr)], src)
    if p.get("highCutHz"):
        src = sos_filter([biquad("lowpass", p["highCutHz"], ctx.sr)], src)
    # y[n] = src[n-d] + fb * y[n-d], computed chunk by chunk (chunk = d)
    for s in range(d, n, d):
        e = min(n, s + d)
        m = e - s
        prev = wet[:, s - d: s - d + m]
        fed = src[:, s - d: s - d + m] + fb * prev
        if ping:
            fed = fed[::-1]
        wet[:, s:e] = fed
    mix = ctx.curves.get("mix", p.get("mix", 0.25))
    return x * (1 - mix) + wet * mix


def fx_chorus(x, p, ctx):
    import pedalboard
    ch = pedalboard.Chorus(rate_hz=p.get("rate", 1.0), depth=p.get("depth", 0.25), centre_delay_ms=p.get("delayMs", 7.0),
                           feedback=p.get("feedback", 0.0), mix=1.0)
    wet = ch(x.astype(np.float32), ctx.sr).astype(np.float64)
    mix = ctx.curves.get("mix", p.get("mix", 0.5))
    return x * (1 - mix) + wet * mix


def fx_distortion(x, p, ctx):
    drive = ctx.curves.get("driveDb", p.get("driveDb", 12.0))
    y = np.tanh(x * db2lin(drive)) * db2lin(p.get("outputDb", -6.0))
    mix = ctx.curves.get("mix", p.get("mix", 1.0))
    return x * (1 - mix) + y * mix


def fx_utility(x, p, ctx):
    g = ctx.curves.get("gainDb", p.get("gainDb", 0.0))
    y = x * db2lin(g)
    if p.get("polarity", False):
        y = -y
    w = ctx.curves.get("width", p.get("width", 1.0))
    mid, side = (y[0] + y[1]) / 2, (y[0] - y[1]) / 2 * w
    if p.get("mono", False):
        side = side * 0
    y = np.vstack([mid + side, mid - side])
    if p.get("bassMonoHz"):
        lo = sos_filter([biquad("lowpass", p["bassMonoHz"], ctx.sr)] * 2, y)
        hi = y - lo
        lm = (lo[0] + lo[1]) / 2
        y = hi + np.vstack([lm, lm])
    return y


def fx_volumeshaper(x, p, ctx):
    """Tempo-synced ducking ("pump"): gain dips to -depthDb at every `rateBeats`
    (quarter notes) and recovers with a curve. Classic sidechain-style pumping."""
    rate = p.get("rateBeats", 1.0)
    depth = p.get("depthDb", 12.0)
    hold, rec = p.get("hold", 0.05), p.get("recover", 0.55)
    beats = ctx.beat_at_sample(np.arange(x.shape[1]))
    ph = (beats / rate) % 1.0
    shape = np.where(ph < hold, 0.0, np.clip((ph - hold) / max(1e-6, rec), 0, 1) ** p.get("curve", 1.5))
    g = db2lin(-depth * (1 - shape))
    mix = ctx.curves.get("mix", p.get("mix", 1.0))
    return x * (1 - mix) + x * g * mix


FX = {"eq": fx_eq, "filter": fx_filter, "compressor": fx_compressor, "limiter": fx_limiter, "gate": fx_gate,
      "reverb": fx_reverb, "delay": fx_delay, "chorus": fx_chorus, "distortion": fx_distortion,
      "utility": fx_utility, "volumeshaper": fx_volumeshaper}

INSTRUMENTS = ("synth", "drums", "sampler")

PARAMS = {
    "synth": {k: v for k, v in SYNTH_DEFAULTS.items()} | {"_doc": "subtractive synth; wave saw|square|sine|triangle|noise; voices>1 = unison/supersaw"},
    "drums": {"gainDb": -3.0, "tune": 0.0, "kickPitch": 50.0, "kickDecay": 0.45, "snareDecay": 0.22,
              "hatDecay": 0.05, "openHatDecay": 0.35, "crashDecay": 1.6,
              "_doc": "GM keys: 36 kick, 37 rim, 38 snare, 39 clap, 42 hat, 46 open hat, 41-50 toms, 49 crash, 51 ride"},
    "sampler": {"file": "", "root": 60, "mode": "oneshot", "attack": 0.0, "release": 0.05, "tune": 0.0, "gainDb": 0.0,
                "_doc": "plays a media file pitched from root; mode oneshot|sustain; zones: [{file, keyLo, keyHi, root}]"},
    "eq": {"lowCutHz": 0, "highCutHz": 0, "lowShelfHz": 120, "lowShelfDb": 0, "highShelfHz": 8000, "highShelfDb": 0,
           "peak1Hz": 1000, "peak1Db": 0, "peak1Q": 1.0, "peak2Hz": 3000, "peak2Db": 0, "peak2Q": 1.0,
           "peak3Hz": 300, "peak3Db": 0, "peak3Q": 1.0, "steep": False, "_auto": ["lowCutHz", "highCutHz"]},
    "filter": {"type": "lowpass", "cutoff": 1000.0, "resonance": 0.0, "mix": 1.0, "_auto": ["cutoff", "mix"]},
    "compressor": {"thresholdDb": -18.0, "ratio": 4.0, "kneeDb": 6.0, "attack": 0.005, "release": 0.12,
                   "makeupDb": 0.0, "mix": 1.0, "_doc": "set sidechain: <track id> to duck from another track", "_auto": ["mix"]},
    "limiter": {"ceilingDb": -1.0, "gainDb": 0.0, "lookaheadMs": 1.5, "release": 0.08, "_doc": "true-peak aware brickwall"},
    "gate": {"thresholdDb": -50.0, "attack": 0.001, "release": 0.05, "floorDb": -80.0},
    "reverb": {"size": 0.7, "damping": 0.5, "width": 1.0, "predelayMs": 0.0, "lowCutHz": 0, "mix": 0.3, "_auto": ["mix"]},
    "delay": {"timeBeats": 0.75, "timeMs": 375.0, "feedback": 0.35, "pingpong": False, "lowCutHz": 0, "highCutHz": 0,
              "mix": 0.25, "_auto": ["mix"]},
    "chorus": {"rate": 1.0, "depth": 0.25, "delayMs": 7.0, "feedback": 0.0, "mix": 0.5, "_auto": ["mix"]},
    "distortion": {"driveDb": 12.0, "outputDb": -6.0, "mix": 1.0, "_auto": ["driveDb", "mix"]},
    "utility": {"gainDb": 0.0, "width": 1.0, "mono": False, "polarity": False, "bassMonoHz": 0, "_auto": ["gainDb", "width"]},
    "volumeshaper": {"rateBeats": 1.0, "depthDb": 12.0, "hold": 0.05, "recover": 0.55, "curve": 1.5, "mix": 1.0,
                     "_auto": ["mix"], "_doc": "tempo-synced ducking (EDM pump)"},
}
