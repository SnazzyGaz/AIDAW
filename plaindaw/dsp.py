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
                  "pitchDecay": 0.05, "mono": False, "lfoRate": 0.0, "lfoPitch": 0.0, "lfoCutoff": 0.0,
                  "lfoAmp": 0.0, "fmRatio": 0.0, "fmIndex": 0.0, "fmDecay": 0.0, "noise": 0.0, "drift": 0.0,
                  "formant": "", "formantMix": 1.0}


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
    lfo_rate = float(p.get("lfoRate", 0.0) or 0.0)
    lfo = np.sin(TWO_PI * lfo_rate * t) if lfo_rate > 0 else None
    if lfo is not None and p.get("lfoPitch"):
        pitch = pitch * 2.0 ** (float(p["lfoPitch"]) * lfo / 12.0)
    if p.get("drift"):                     # slow random pitch wander (cents), e.g. an unsteady motor
        k = max(2, int(n / sr * 4) + 2)
        pts = rng.normal(0, 1, k)
        pitch = pitch * 2.0 ** (float(p["drift"]) * np.interp(np.arange(n), np.linspace(0, n, k), pts) / 1200.0)
    fm_ratio, fm_index = float(p.get("fmRatio", 0.0) or 0.0), float(p.get("fmIndex", 0.0) or 0.0)
    out = np.zeros((2, n))
    for v in range(voices):
        spread = 0.0 if voices == 1 else (v / (voices - 1)) * 2 - 1
        cents = spread * p["detune"]
        f = np.full(n, f0 * 2 ** (cents / 1200.0)) * pitch
        if fm_ratio > 0 and fm_index:      # 2-op FM: a sine modulator at f*ratio bends the carrier
            idx = fm_index * (np.exp(-t / float(p["fmDecay"])) if p.get("fmDecay") else 1.0)
            mod = np.sin(TWO_PI * np.cumsum(f * fm_ratio) / sr)
            f = f * (1.0 + idx * mod * fm_ratio)
            f = np.abs(f)
        s = osc(p["wave"], f, sr, rng.uniform(0, 1) if voices > 1 else 0.0, rng)
        if p.get("noise"):
            s = s * (1 - float(p["noise"])) + rng.uniform(-1, 1, n) * float(p["noise"])
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
    if lfo is not None and p.get("lfoCutoff"):
        base = cut if cut is not None else np.full(n, float(cutoff))
        cut = np.clip(base * 2.0 ** (float(p["lfoCutoff"]) * lfo), 20, sr * 0.45)
    q = 0.707 + p["resonance"] * 8
    if cut is not None:
        out = timevarying("lowpass", out, sr, cut, q)
    elif cutoff < sr * 0.45:
        out = sos_filter([biquad("lowpass", cutoff, sr, q)], out)
    if p.get("formant"):
        out = formant_filter(out, sr, str(p["formant"]), float(p.get("formantMix", 1.0)))
    if lfo is not None and p.get("lfoAmp"):
        out = out * (1.0 - float(p["lfoAmp"]) * 0.5 * (1 - lfo))
    return out * env * (vel / 127.0) * db2lin(p["gainDb"])


# ---------------------------------------------------------------- vowels, bells, machinery
FORMANTS = {  # (Hz, dB) for F1..F4 of a low male voice
    "a": ((730, 0), (1090, -5), (2440, -15), (3400, -22)),
    "o": ((570, 0), (840, -4), (2410, -20), (3400, -26)),
    "u": ((300, 0), (870, -10), (2240, -24), (3300, -30)),
    "e": ((530, 0), (1840, -8), (2480, -12), (3500, -20)),
    "i": ((270, 0), (2290, -10), (3010, -14), (3600, -20)),
}


def formant_filter(x, sr, vowel="a", mix=1.0, q=8.0):
    """Parallel band-passes at a vowel's formants: turns a buzzy saw/noise into an 'ahh'/'ooh' choir tone.
    vowel: a|e|i|o|u, or two letters ('ao') for the halfway blend."""
    v = (vowel or "a").lower()
    tabs = [FORMANTS[c] for c in v if c in FORMANTS] or [FORMANTS["a"]]
    if len(tabs) > 1:
        tab = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(tabs[0], tabs[1])]
    else:
        tab = tabs[0]
    y = np.zeros_like(x)
    for f, g in tab:
        if f < sr * 0.45:
            y += sos_filter([biquad("bandpass", f, sr, q)], x) * db2lin(g)
    y *= 2.5
    return x * (1 - mix) + y * mix


BELL_PARTIALS = ((0.5, 1.0, 1.0), (1.0, 0.8, 0.75), (1.183, 0.6, 0.55), (1.506, 0.45, 0.45), (2.0, 0.7, 0.4),
                 (2.514, 0.35, 0.3), (2.662, 0.3, 0.25), (3.011, 0.25, 0.2), (4.166, 0.2, 0.14), (5.433, 0.12, 0.09))


def bell_note(p: dict, key: int, vel: float, sr: float, rng: np.random.Generator) -> np.ndarray:
    """Church-bell additive synthesis: hum, prime, minor-third 'tierce', quint, nominal and inharmonic upper
    partials, each with its own decay and a slight beat between the left and right copies. `key` is the nominal."""
    dec = float(p.get("decay", 6.0))
    n = int((dec * 1.2 + 0.05) * sr)
    t = np.arange(n) / sr
    f0 = mtof(key + 12 * int(p.get("octave", 0))) / 2.0          # hum note an octave below the nominal
    bright = float(p.get("brightness", 0.5))
    out = np.zeros((2, n))
    for i, (ratio, amp, dk) in enumerate(BELL_PARTIALS):
        f = f0 * ratio * 2.0
        if f > sr * 0.45:
            continue
        a = amp * (bright ** (i / 4.0) if i > 4 else 1.0)
        e = np.exp(-t / max(0.05, dec * dk))
        beat = float(p.get("beat", 0.6))
        for ch, sgn in ((0, 1), (1, -1)):
            out[ch] += a * np.sin(TWO_PI * (f + sgn * beat * (i % 3)) * t + rng.uniform(0, TWO_PI)) * e
    strike = rng.uniform(-1, 1, n) * np.exp(-t / 0.004) * float(p.get("strike", 0.3))
    strike = sos_filter([biquad("bandpass", min(f0 * 8, sr * 0.4), sr, 1.5)], strike)
    out += strike
    out /= 3.0
    return out * (vel / 127.0) * db2lin(p.get("gainDb", -6.0))


def _modal(freqs, amps, decays, n, sr, rng, jitter=0.0):
    t = np.arange(n) / sr
    s = np.zeros(n)
    for f, a, d in zip(freqs, amps, decays):
        f = f * (1 + rng.uniform(-jitter, jitter))
        if f < sr * 0.45:
            s += a * np.sin(TWO_PI * f * t + rng.uniform(0, TWO_PI)) * np.exp(-t / d)
    return s


def foundry_hit(key: int, vel: float, sr: float, rng: np.random.Generator, p: dict) -> np.ndarray:
    """Synthesised heavy machinery (all original, no samples):
    36 distorted sub kick, 37 piston thump, 38 anvil clank, 39 metal slam, 40 hydraulic hiss,
    41 ratchet, 42 gear tick, 43 chain rattle, 44 steam release, 45 metal scrape, 46 sheet-metal hit."""
    tune = 2 ** (p.get("tune", 0.0) / 12)
    g = (vel / 127.0) * db2lin(p.get("gainDb", -3.0))
    drive = db2lin(p.get("drive", 6.0))
    size = float(p.get("size", 1.0))                      # bigger = lower, longer metal
    def nz(m):
        return rng.uniform(-1, 1, m)
    def bp(x, f, q=1.0):
        return sos_filter([biquad("bandpass", min(f, sr * 0.45), sr, q)], x)
    if key == 36:                                          # big distorted sub kick
        dec = p.get("kickDecay", 0.7); n = int(dec * sr); t = np.arange(n) / sr
        f = p.get("kickPitch", 42.0) * tune * (1 + 4.0 * np.exp(-t / 0.03))
        body = np.sin(TWO_PI * np.cumsum(f) / sr) * np.exp(-t / (dec / 2.5))
        click = bp(nz(n), 3000, 0.8) * np.exp(-t / 0.004) * 0.6
        s = np.tanh(body * 1.2 * drive) * 0.85
        s = sos_filter([biquad("lowpass", 900, sr, 0.6)], s) + click * 0.5
    elif key == 37:                                        # piston thump + exhaust chuff
        n = int(0.45 * sr); t = np.arange(n) / sr
        thud = np.sin(TWO_PI * np.cumsum(70 * tune * (1 + 1.5 * np.exp(-t / 0.02))) / sr) * np.exp(-t / 0.08)
        chuff = bp(nz(n), 900 * tune, 0.7) * np.exp(-((t - 0.05) / 0.05) ** 2) * 0.6
        s = sos_filter([biquad("lowpass", 1400, sr, 0.6)], np.tanh(thud * 1.2 * drive)) * 0.8 + chuff * 0.5
    elif key in (38, 39, 46):                              # anvil clank / big slam / sheet metal
        base = {38: 520.0, 39: 190.0, 46: 330.0}[key] * tune / size
        dur = {38: 0.9, 39: 2.2, 46: 1.6}[key] * size; n = int(dur * sr); t = np.arange(n) / sr
        ratios = (1.0, 2.76, 5.40, 8.93, 13.34, 1.59, 3.9) if key != 46 else (1.0, 1.41, 2.13, 2.97, 3.67, 4.61, 6.2)
        amps = (1.0, 0.7, 0.5, 0.35, 0.2, 0.5, 0.3)
        decs = tuple(dur / (2.5 + 1.2 * i) for i in range(len(ratios)))
        s = _modal([base * r for r in ratios], amps, decs, n, sr, rng, 0.01)
        hit = bp(nz(n), 2500, 0.6) * np.exp(-t / 0.006) * 1.2
        s = s * 0.5 + hit
        if key == 39:
            s += np.sin(TWO_PI * np.cumsum(55 * tune * (1 + 2 * np.exp(-t / 0.03))) / sr) * np.exp(-t / 0.15) * 0.9
        s = np.tanh(s * 1.3 * drive) * 0.7
    elif key == 40:                                        # hydraulic hiss (swelling band-passed noise)
        n = int(0.8 * sr); t = np.arange(n) / sr
        env = np.minimum(1, t / 0.08) * np.exp(-np.maximum(0, t - 0.1) / 0.25)
        cut = 1800 * tune * (1 + 1.5 * t / 0.8)
        s = timevarying("bandpass", nz(n)[None, :].repeat(2, 0), sr, cut, 1.2)[0] * env * 0.9
    elif key in (41, 42):                                  # ratchet train / single gear tick
        clicks = int(p.get("ratchetClicks", 8)) if key == 41 else 1
        gap = float(p.get("ratchetGap", 0.028))
        n = int((clicks * gap + 0.08) * sr); s = np.zeros(n)
        for c in range(clicks):
            o = int(c * gap * (1 + rng.uniform(-0.08, 0.08)) * sr); m = min(n - o, int(0.03 * sr))
            tt = np.arange(m) / sr
            tick = _modal([2300 * tune, 3700 * tune, 5600 * tune], [1, 0.6, 0.4], [0.008, 0.006, 0.004], m, sr, rng, 0.05)
            tick += nz(m) * np.exp(-tt / 0.0015) * 0.8
            s[o:o + m] += tick * (0.7 + 0.3 * rng.uniform())
        s *= 0.6
    elif key == 43:                                        # chain rattle: cluster of small metal pings
        n = int(0.6 * sr); s = np.zeros(n)
        for _ in range(int(p.get("chainLinks", 22))):
            o = int(abs(rng.normal(0.12, 0.1)) * sr) % (n - int(0.06 * sr)); m = int(0.06 * sr)
            f = rng.uniform(1800, 4200) * tune
            s[o:o + m] += _modal([f, f * 1.73, f * 2.41], [1, 0.5, 0.3], [0.012, 0.008, 0.006], m, sr, rng, 0.02) * rng.uniform(0.3, 1)
        s *= 0.35
    elif key == 44:                                        # steam release
        n = int(1.4 * sr); t = np.arange(n) / sr
        env = np.minimum(1, t / 0.02) * np.exp(-t / 0.5)
        s = sos_filter([biquad("highpass", 2500 * tune, sr), biquad("peak", 5000 * tune, sr, 2, 6)], nz(n)) * env * 0.5
    elif key == 45:                                        # metal scrape / grind
        n = int(1.0 * sr); t = np.arange(n) / sr
        fm = 600 * tune * (1 + 0.3 * np.sin(TWO_PI * 13 * t)) * (1 + 0.15 * nz(n))
        car = np.sign(np.sin(TWO_PI * np.cumsum(fm) / sr))
        s = bp(car * 0.5 + nz(n) * 0.5, 1800 * tune, 2.0) * np.sin(np.pi * t / 1.0) * 0.8
        s = np.tanh(s * drive)
    else:
        return np.zeros((2, 1))
    s = s * g
    # tiny stereo offset so stacks of metal hits don't collapse to mono
    d = int(float(p.get("spreadMs", 0.4)) * sr / 1000)
    r = np.concatenate([np.zeros(d), s[:len(s) - d]]) if d > 0 else s
    return np.vstack([s, r])


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


def fx_bitcrush(x, p, ctx):
    """Lo-fi: quantise to `bits` and sample-and-hold every `downsample` samples."""
    bits = float(ctx.curves.get("bits", p.get("bits", 8)))
    ds = max(1, int(p.get("downsample", 4)))
    y = x * db2lin(p.get("driveDb", 0.0))
    if ds > 1:
        idx = (np.arange(x.shape[1]) // ds) * ds
        y = y[:, idx]
    steps = 2.0 ** (max(1.0, float(np.mean(bits))) - 1)
    y = np.round(y * steps) / steps
    mix = ctx.curves.get("mix", p.get("mix", 1.0))
    return x * (1 - mix) + y * mix


def fx_formant(x, p, ctx):
    return formant_filter(x, ctx.sr, str(p.get("vowel", "a")), float(ctx.curves.get("mix", p.get("mix", 1.0))),
                          float(p.get("q", 8.0)))


def fx_saturator(x, p, ctx):
    """Warm asymmetric tube-style saturation with a tone tilt (less fizzy than distortion)."""
    drive = db2lin(ctx.curves.get("driveDb", p.get("driveDb", 9.0)))
    bias = float(p.get("bias", 0.15))
    y = np.tanh(x * drive + bias) - np.tanh(bias)
    if p.get("toneHz"):
        y = sos_filter([biquad("lowpass", p["toneHz"], ctx.sr)], y)
    y *= db2lin(p.get("outputDb", -4.0))
    mix = ctx.curves.get("mix", p.get("mix", 1.0))
    return x * (1 - mix) + y * mix


FX = {"eq": fx_eq, "filter": fx_filter, "compressor": fx_compressor, "limiter": fx_limiter, "gate": fx_gate,
      "reverb": fx_reverb, "delay": fx_delay, "chorus": fx_chorus, "distortion": fx_distortion,
      "utility": fx_utility, "volumeshaper": fx_volumeshaper, "bitcrush": fx_bitcrush, "formant": fx_formant,
      "saturator": fx_saturator}

INSTRUMENTS = ("synth", "drums", "sampler", "foundry", "bell")

PARAMS = {
    "synth": {k: v for k, v in SYNTH_DEFAULTS.items()} | {"_doc": "subtractive synth; wave saw|square|sine|triangle|noise; voices>1 = unison/supersaw; lfoRate Hz with lfoPitch (semitones), lfoCutoff (octaves), lfoAmp (0-1); fmRatio/fmIndex/fmDecay = 2-op FM; noise 0-1; drift (cents); formant a|e|i|o|u"},
    "drums": {"gainDb": -3.0, "tune": 0.0, "kickPitch": 50.0, "kickDecay": 0.45, "snareDecay": 0.22,
              "hatDecay": 0.05, "openHatDecay": 0.35, "crashDecay": 1.6,
              "_doc": "GM keys: 36 kick, 37 rim, 38 snare, 39 clap, 42 hat, 46 open hat, 41-50 toms, 49 crash, 51 ride"},
    "foundry": {"gainDb": -3.0, "tune": 0.0, "drive": 6.0, "size": 1.0, "kickPitch": 42.0, "kickDecay": 0.7,
                "ratchetClicks": 8, "ratchetGap": 0.028, "chainLinks": 22, "spreadMs": 0.4,
                "_doc": "industrial machinery kit: 36 distorted sub kick, 37 piston, 38 anvil, 39 metal slam, 40 hydraulic "
                        "hiss, 41 ratchet, 42 gear tick, 43 chain rattle, 44 steam, 45 scrape/grind, 46 sheet metal"},
    "bell": {"gainDb": -6.0, "decay": 6.0, "brightness": 0.5, "beat": 0.6, "strike": 0.3, "octave": 0,
             "_doc": "church/tolling bell (additive, inharmonic partials); key = nominal pitch"},
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
    "bitcrush": {"bits": 8, "downsample": 4, "driveDb": 0.0, "mix": 1.0, "_auto": ["bits", "mix"]},
    "formant": {"vowel": "a", "q": 8.0, "mix": 1.0, "_auto": ["mix"], "_doc": "vowel filter a|e|i|o|u (or 'ao' blend)"},
    "saturator": {"driveDb": 9.0, "bias": 0.15, "toneHz": 0, "outputDb": -4.0, "mix": 1.0, "_auto": ["driveDb", "mix"]},
    "volumeshaper": {"rateBeats": 1.0, "depthDb": 12.0, "hold": 0.05, "recover": 0.55, "curve": 1.5, "mix": 1.0,
                     "_auto": ["mix"], "_doc": "tempo-synced ducking (EDM pump)"},
}
