"""The listening module: musical features an AI can read instead of hearing.

Pure numpy/scipy (deterministic, no extra downloads): key + chords from chroma,
tempo + beat grid from an onset envelope, onset timing vs the grid, transient
strength, timbre tags per track, energy curve, and reference-track comparison.
Extra listeners (e.g. an audio-text embedding model) plug in via PLAINDAW_LISTENERS.
"""
from __future__ import annotations

import importlib
import math
import os
from fractions import Fraction

import numpy as np
from scipy import signal

from .theory import NAMES_FLAT, NAMES_SHARP, chord_symbol

SR = 22050
N_FFT = 4096      # harmony analysis
HOP = 512
ON_N = 1024       # onset / rhythm analysis
ON_HOP = 256

# Krumhansl-Kessler key profiles
KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _r(x, d=2):
    if x is None:
        return None
    x = float(x)
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, d)


def _mono22(x: np.ndarray, sr: int) -> np.ndarray:
    m = np.asarray(x, dtype=np.float64)
    if m.ndim > 1:
        m = m.mean(axis=0)
    if sr != SR:
        fr = Fraction(SR, sr).limit_denominator(1000)
        m = signal.resample_poly(m, fr.numerator, fr.denominator)
    return m


def _stft(m: np.ndarray) -> np.ndarray:
    if m.size < N_FFT:
        m = np.pad(m, (0, N_FFT - m.size))
    _, _, Z = signal.stft(m, SR, nperseg=N_FFT, noverlap=N_FFT - HOP, boundary=None, padded=True)
    return np.abs(Z)  # (freq, frames)


def chroma(mag: np.ndarray) -> np.ndarray:
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    ok = (freqs >= 55) & (freqs <= 4200)
    midi = 69 + 12 * np.log2(freqs[ok] / 440.0)
    pc = np.mod(np.round(midi), 12).astype(int)
    # weight lower-mid region more (fundamentals), compress magnitudes
    w = 1.0 / np.sqrt(1 + (freqs[ok] / 1500.0) ** 2)
    M = np.log1p(100 * mag[ok]) * w[:, None]
    C = np.zeros((12, mag.shape[1]))
    for p in range(12):
        C[p] = M[pc == p].sum(axis=0)
    return C


def estimate_key(C: np.ndarray) -> dict:
    prof = C.sum(axis=1)
    if prof.sum() <= 1e-9:
        return {"key": None, "confidence": 0.0}
    scores = []
    for t in range(12):
        for name, kk in (("major", KK_MAJOR), ("minor", KK_MINOR)):
            r = np.corrcoef(prof, np.roll(kk, t))[0, 1]
            scores.append((r, t, name))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    flats = best[2] == "minor" or best[1] in (5, 10, 3, 8, 1)
    nm = (NAMES_FLAT if flats else NAMES_SHARP)[best[1]]
    return {"key": f"{nm} {best[2]}", "tonic": best[1], "scale": best[2], "confidence": _r(best[0], 3),
            "runnerUp": f"{(NAMES_FLAT if flats else NAMES_SHARP)[second[1]]} {second[2]}",
            "chromaProfile": {NAMES_SHARP[i]: _r(v / prof.max(), 2) for i, v in enumerate(prof)}}


def rescore_key(C: np.ndarray, chords: list, first: dict) -> dict:
    """Blend the profile match with harmony: keys whose tonic chord is heard often, and whose
    scale contains the heard chords, win close calls (fixes relative/fifth confusions)."""
    from .theory import parse_chord, scale_pcs
    heard = [c for c in chords if c and c != "-"]
    if not heard or not first.get("key"):
        return first
    prof = C.sum(axis=1)
    cands = []
    for t in range(12):
        for name, kk in (("major", KK_MAJOR), ("minor", KK_MINOR)):
            r = float(np.corrcoef(prof, np.roll(kk, t))[0, 1])
            pcs = set(scale_pcs(t, name))
            tonic_q = "m" if name == "minor" else ""
            n_tonic = n_diat = 0
            for c in heard:
                root, ivs, q = parse_chord(c)
                if root == t and ((q == "m") == (tonic_q == "m")):
                    n_tonic += 1
                if all((root + i) % 12 in pcs for i in ivs):
                    n_diat += 1
            first_bonus = 0.05 if heard[0] == chord_symbol(t, tonic_q) else 0.0
            score = r + 0.25 * n_tonic / len(heard) + 0.15 * n_diat / len(heard) + first_bonus
            cands.append((score, r, t, name))
    cands.sort(reverse=True)
    sc, r, t, name = cands[0]
    flats = name == "minor" or t in (5, 10, 3, 8, 1)
    names = NAMES_FLAT if flats else NAMES_SHARP
    out = dict(first)
    out.update({"key": f"{names[t]} {name}", "tonic": t, "scale": name, "confidence": _r(r, 3),
                "runnerUp": f"{(NAMES_FLAT if cands[1][3] == 'minor' else NAMES_SHARP)[cands[1][2]]} {cands[1][3]}",
                "method": "chroma profile + heard chords"})
    return out


TRIADS = []
for _root in range(12):
    for _q, _iv in (("", (0, 4, 7)), ("m", (0, 3, 7))):
        v = np.zeros(12)
        for i in _iv:
            v[(_root + i) % 12] = 1
        v[_root] = 1.3  # root emphasis
        TRIADS.append((_root, _q, v / np.linalg.norm(v)))


def chord_at(c: np.ndarray) -> tuple[str | None, float]:
    if c.sum() <= 1e-9:
        return None, 0.0
    c = c - c.mean()
    n = np.linalg.norm(c)
    if n < 1e-9:
        return None, 0.0
    c = c / n
    best = max(TRIADS, key=lambda t: float(np.dot(c, t[2] - t[2].mean())))
    return chord_symbol(best[0], best[1]), float(np.dot(c, best[2] - best[2].mean()))


def onset_envelope(m: np.ndarray) -> np.ndarray:
    if m.size < ON_N:
        m = np.pad(m, (0, ON_N - m.size))
    _, _, Z = signal.stft(m, SR, nperseg=ON_N, noverlap=ON_N - ON_HOP, boundary=None, padded=True)
    L = np.log1p(1000 * np.abs(Z))
    d = np.diff(L, axis=1, prepend=L[:, :1])
    env = np.maximum(d, 0).sum(axis=0)
    if env.max() > 0:
        env = env / env.max()
    return env


def estimate_tempo(env: np.ndarray, lo=60, hi=200, hint: float | None = None) -> dict:
    fps = SR / ON_HOP
    e = env - env.mean()
    if np.max(np.abs(e)) < 1e-9 or e.size < 16:
        return {"bpm": None, "confidence": 0.0}
    ac = np.correlate(e, e, mode="full")[e.size - 1:]
    ac /= ac[0] + 1e-12
    bpms = np.arange(lo, hi + 0.01, 0.25)
    lags = 60 * fps / bpms
    vals = np.interp(lags, np.arange(ac.size), ac)
    # add half/double-tempo support and a gentle prior around 120 bpm
    v2 = np.interp(lags * 2, np.arange(ac.size), ac)
    score = vals + 0.5 * v2
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)
    score = score * (0.6 + 0.4 * prior)
    i = int(np.argmax(score))
    bpm = float(bpms[i])
    conf = float(vals[i])
    if hint:
        # prefer the hinted tempo family if it scores close
        cand = [hint, hint * 2, hint / 2]
        for c in cand:
            if lo <= c <= hi:
                j = int(np.argmin(np.abs(bpms - c)))
                if score[j] >= 0.8 * score[i]:
                    bpm, conf = float(c), float(vals[j])
                    break
    return {"bpm": _r(bpm, 2), "confidence": _r(conf, 3)}


def beat_grid(env: np.ndarray, bpm: float) -> dict:
    fps = SR / ON_HOP
    period = 60 * fps / bpm
    n = env.size
    best, best_off = -1, 0.0
    for off in np.arange(0, period, 0.25):
        idx = np.round(np.arange(off, n, period)).astype(int)
        idx = idx[idx < n]
        s = env[idx].sum()
        if s > best:
            best, best_off = s, off
    beats = np.arange(best_off, n, period) / fps
    return {"firstBeatSec": _r(best_off / fps, 3), "beats": int(beats.size), "periodSec": _r(60 / bpm, 4)}


def onsets(env: np.ndarray) -> np.ndarray:
    fps = SR / ON_HOP
    if env.max() <= 0:
        return np.array([])
    pk, _ = signal.find_peaks(env, height=0.12, distance=max(1, int(0.05 * fps)), prominence=0.08)
    return pk


def refine_onsets(m: np.ndarray, pk: np.ndarray) -> np.ndarray:
    """Sample-accurate onset times: steepest rise of a 1 ms envelope near each frame peak."""
    a = np.abs(m)
    k = max(1, int(0.001 * SR))
    envf = np.convolve(a, np.ones(k) / k, mode="same")
    d = np.diff(envf, prepend=envf[:1])
    out = []
    for p in pk:
        c = int(p * ON_HOP + ON_N // 2)
        lo, hi = max(0, c - ON_N // 2), min(d.size, c + ON_HOP)
        if hi - lo < 4:
            out.append(c / SR)
            continue
        out.append((lo + int(np.argmax(d[lo:hi]))) / SR)
    return np.array(out)


def timing(env: np.ndarray, bpm: float, first_beat: float, grid_div: int = 4, m: np.ndarray | None = None) -> dict:
    """Deviation of each onset from the nearest 1/16 grid line (grid_div per beat)."""
    fps = SR / ON_HOP
    pk = onsets(env)
    if pk.size < 4 or not bpm:
        return {"onsets": int(pk.size), "medianDevMs": None}
    t = refine_onsets(m, pk) if m is not None else pk / fps
    if m is not None:
        # joint fine search of tempo (+-1 BPM) and grid phase on sample-accurate onsets
        best = None
        for b in np.arange(bpm - 1.0, bpm + 1.0001, 0.02):
            st = 60.0 / b / grid_div
            ph = np.arange(0, st, 0.0005)[:, None]
            rel = (t[None, :] - ph) / st
            cost = np.median(np.abs(rel - np.round(rel)), axis=1)
            k = int(np.argmin(cost))
            if best is None or cost[k] < best[0] - 1e-12:
                best = (float(cost[k]), float(b), float(ph[k, 0]))
        bpm, first_beat = best[1], best[2]
    step = 60.0 / bpm / grid_div
    rel = (t - first_beat) / step
    dev = (rel - np.round(rel)) * step * 1000.0
    a = np.abs(dev)
    # frames are ~23 ms, so sub-frame accuracy is limited; refine with parabolic interpolation
    return {"onsets": int(pk.size), "medianDevMs": _r(np.median(a), 1), "p90DevMs": _r(np.percentile(a, 90), 1),
            "within10ms": _r(np.mean(a <= 10.0), 3), "meanSignedMs": _r(np.mean(dev), 1),
            "feel": ("tight" if np.median(a) < 4 else "loose" if np.median(a) > 12 else "natural"),
            "gridBpm": _r(bpm, 2)}


def transients(x_mono: np.ndarray) -> dict:
    """Transient strength: fast vs slow envelope ratio at peaks (dB)."""
    if x_mono.size < SR // 10 or np.max(np.abs(x_mono)) < 1e-6:
        return {"strengthDb": None, "character": "silent"}
    a = np.abs(x_mono)
    fast = signal.lfilter([1 - math.exp(-1 / (0.001 * SR))], [1, -math.exp(-1 / (0.001 * SR))], a)
    slow = signal.lfilter([1 - math.exp(-1 / (0.08 * SR))], [1, -math.exp(-1 / (0.08 * SR))], a)
    ratio = fast / (slow + 1e-9)
    pk, _ = signal.find_peaks(ratio, distance=int(0.05 * SR))
    if pk.size == 0:
        return {"strengthDb": 0.0, "character": "smooth"}
    top = np.sort(ratio[pk])[-max(1, pk.size // 4):]
    db = 20 * math.log10(float(np.median(top)) + 1e-9)
    ch = "punchy" if db > 9 else "soft" if db < 4 else "moderate"
    return {"strengthDb": _r(db, 1), "character": ch}


def timbre(x: np.ndarray, sr: int) -> dict:
    """Spectral features + plain tags for one stereo signal."""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = np.vstack([x, x])
    if np.max(np.abs(x)) < 1e-7:
        return {"tags": ["silent"]}
    mono = x.mean(axis=0)
    f, p = signal.welch(mono, sr, nperseg=4096 if mono.size >= 4096 else max(256, mono.size))
    tot = p.sum() + 1e-20
    def band(lo, hi):
        return float(p[(f >= lo) & (f < hi)].sum() / tot)
    centroid = float((f * p).sum() / tot)
    cum = np.cumsum(p) / tot
    rolloff = float(f[min(np.searchsorted(cum, 0.85), f.size - 1)])
    sel = (f > 50) & (f < 12000)
    flat = float(np.exp(np.mean(np.log(p[sel] + 1e-20))) / (np.mean(p[sel]) + 1e-20)) if sel.any() else 0.0
    sub, low, lowmid, mid, harsh, air = band(20, 60), band(60, 250), band(250, 500), band(500, 2000), band(2000, 5000), band(8000, 20000)
    L, R = x[0], x[1]
    ms = float(np.mean(((L + R) / 2) ** 2)); ss = float(np.mean(((L - R) / 2) ** 2))
    width = math.sqrt(ss / ms) if ms > 1e-20 else 0.0
    tags = []
    if centroid > 3500 or air > 0.08:
        tags.append("bright")
    elif centroid < 600:
        tags.append("dark")
    if air > 0.12:
        tags.append("airy")
    if lowmid > 0.28 and centroid < 1500:
        tags.append("muddy")
    if harsh > 0.30:
        tags.append("harsh")
    if sub > 0.35:
        tags.append("subby")
    elif low + sub > 0.6:
        tags.append("bassy")
    if low + sub < 0.03 and centroid > 1500:
        tags.append("thin")
    if width > 0.6:
        tags.append("wide")
    elif width < 0.05:
        tags.append("mono")
    if flat > 0.35:
        tags.append("noisy")
    elif flat < 0.05:
        tags.append("tonal")
    tr = transients(_mono22(x, sr))
    if tr["character"] == "punchy":
        tags.append("punchy")
    elif tr["character"] == "soft":
        tags.append("soft")
    return {"centroidHz": _r(centroid, 0), "rolloffHz": _r(rolloff, 0), "flatness": _r(flat, 3),
            "width": _r(width, 3), "bandShare": {"sub": _r(sub, 3), "low": _r(low, 3), "lowmid": _r(lowmid, 3),
                                                 "mid": _r(mid, 3), "presence": _r(harsh, 3), "air": _r(air, 3)},
            "transientDb": tr["strengthDb"], "tags": tags}


def energy_curve(x: np.ndarray, sr: int, seg_sec: float) -> list:
    mono = np.asarray(x, dtype=np.float64)
    mono = mono.mean(axis=0) if mono.ndim > 1 else mono
    n = max(1, int(round(seg_sec * sr)))
    out = []
    for s in range(0, mono.size, n):
        seg = mono[s:s + n]
        if seg.size < n // 4:
            break
        rms = math.sqrt(float(np.mean(seg ** 2))) if seg.size else 0.0
        out.append(_r(20 * math.log10(rms + 1e-9), 1))
    return out


def listen(x: np.ndarray, sr: int, *, bpm_hint: float | None = None, bar_sec: float | None = None,
           stems: dict | None = None, names: dict | None = None, sections: list | None = None,
           first_beat_sec: float | None = None) -> dict:
    """Full listening report for a stereo mix (+ optional stems)."""
    m = _mono22(x, sr)
    mag = _stft(m)
    C = chroma(mag)
    rep = {"_note": "measured from audio (key/tempo are estimates)"}
    rep["key"] = estimate_key(C)
    env = onset_envelope(m)
    tempo = estimate_tempo(env, hint=bpm_hint)
    rep["tempo"] = tempo
    bpm = tempo["bpm"] or bpm_hint
    if bpm:
        grid = beat_grid(env, bpm)
        if first_beat_sec is not None and bpm_hint and abs(bpm - bpm_hint) < 0.5:
            grid["firstBeatSec"] = _r(first_beat_sec, 3)
        rep["beatGrid"] = grid
        rep["timing"] = timing(env, bpm, grid["firstBeatSec"], m=m)
        gb = rep["timing"].get("gridBpm")
        if gb and tempo.get("bpm"):
            # report the refined tempo, snapped to a round value when it is that close
            snap = round(gb * 2) / 2
            rep["tempo"]["bpm"] = _r(snap if abs(snap - gb) < 0.08 else gb, 2)
    rep["transients"] = transients(m)
    # chords per bar
    bar = bar_sec or (240.0 / bpm if bpm else 2.0)
    fps = SR / HOP
    chords = []
    fpb = max(1, int(round(bar * fps)))
    for i, s in enumerate(range(0, C.shape[1], fpb)):
        seg = C[:, s:s + fpb]
        if seg.shape[1] < fpb // 3:
            break
        sym, conf = chord_at(seg.sum(axis=1))
        if np.sqrt(np.mean(m[int(s * HOP):int((s + fpb) * HOP)] ** 2) if m.size else 0) < 1e-4:
            sym = None
        chords.append(sym if (sym and conf > 0.25) else "-")
    rep["chordsPerBar"] = chords
    rep["key"] = rescore_key(C, chords, rep["key"])
    rep["energyCurveDb"] = energy_curve(x, sr, bar)
    rep["energyResolution"] = f"one value per {'bar' if bar_sec or bpm else '2 s'} ({_r(bar, 3)} s)"
    if sections:
        secs = []
        for name, s, e in sections:
            seg = np.asarray(x)[:, int(s * sr):int(e * sr)]
            if seg.shape[1] < sr // 10:
                continue
            rms = math.sqrt(float(np.mean(seg ** 2)))
            secs.append({"name": name, "rmsDb": _r(20 * math.log10(rms + 1e-9), 1)})
        rep["sectionEnergy"] = secs
    rep["timbre"] = {"mix": timbre(x, sr)}
    if stems:
        for tid, s in stems.items():
            t = timbre(s, sr)
            if "silent" not in t["tags"]:
                rep["timbre"][tid] = t
    rep["summary"] = summarize(rep, names or {})
    extra = run_plugins(x, sr)
    if extra:
        rep["extra"] = extra
    return rep


def summarize(rep: dict, names: dict) -> list[str]:
    out = []
    k = rep.get("key", {})
    if k.get("key"):
        out.append(f"Key sounds like {k['key']} (confidence {k['confidence']}, next best {k.get('runnerUp')}).")
    t = rep.get("tempo", {})
    if t.get("bpm"):
        out.append(f"Tempo about {t['bpm']} BPM.")
    tm = rep.get("timing", {})
    if tm.get("medianDevMs") is not None:
        out.append(f"Timing is {tm['feel']}: hits sit a median {tm['medianDevMs']} ms off the 1/16 grid "
                   f"({int(100 * (tm['within10ms'] or 0))}% within 10 ms).")
    tr = rep.get("transients", {})
    if tr.get("strengthDb") is not None:
        out.append(f"Transients are {tr['character']} ({tr['strengthDb']} dB).")
    ch = [c for c in rep.get("chordsPerBar", []) if c != "-"]
    if ch:
        uniq = []
        for c in ch:
            if not uniq or uniq[-1] != c:
                uniq.append(c)
        out.append("Chords heard (per bar): " + " ".join(uniq[:16]) + (" …" if len(uniq) > 16 else ""))
    for tid, tb in rep.get("timbre", {}).items():
        if tb.get("tags"):
            nm = "Mix" if tid == "mix" else names.get(tid, tid)
            out.append(f"{nm}: {', '.join(tb['tags'])}.")
    return out


# ------------------------------------------------------------------ reference comparison
def _curve_norm(c: list, n=16) -> np.ndarray:
    v = np.array([x if x is not None else -90 for x in c], dtype=float)
    if v.size == 0:
        return np.zeros(n)
    xs = np.linspace(0, 1, v.size)
    return np.interp(np.linspace(0, 1, n), xs, v)


def compare_reference(mix: np.ndarray, ref: np.ndarray, sr: int, ref_sr: int, *, bpm_hint=None) -> dict:
    from .analysis import analyze as measure
    a = measure(mix, sr)
    b = measure(ref, ref_sr)
    la = listen(mix, sr, bpm_hint=bpm_hint)
    lb = listen(ref, ref_sr)
    diffs = []
    ma, mb = a["master"], b["master"]
    if ma["integratedLufs"] is not None and mb["integratedLufs"] is not None:
        d = ma["integratedLufs"] - mb["integratedLufs"]
        if abs(d) >= 1:
            diffs.append(f"Your mix is {abs(d):.1f} LU {'louder' if d > 0 else 'quieter'} than the reference "
                         f"({ma['integratedLufs']} vs {mb['integratedLufs']} LUFS).")
    if ma["truePeakDbtp"] is not None and mb["truePeakDbtp"] is not None and ma["crestDb"] and mb["crestDb"]:
        d = ma["crestDb"] - mb["crestDb"]
        if abs(d) >= 2:
            diffs.append(f"Your mix is {'more dynamic (less compressed)' if d > 0 else 'more squashed'} "
                         f"than the reference (crest {ma['crestDb']} vs {mb['crestDb']} dB).")
    band_d = {}
    for k in a["bandsDbRel"]:
        va, vb = a["bandsDbRel"][k], b["bandsDbRel"][k]
        if va is None or vb is None:
            continue
        band_d[k] = _r(va - vb, 1)
        lo, hi = k.split("_")[1:]
        nm = k.split("_")[0]
        if abs(va - vb) >= 3:
            diffs.append(f"{'More' if va > vb else 'Less'} {nm} ({lo}-{hi} Hz) than the reference by {abs(va - vb):.1f} dB (tonal balance, level-matched).")
    wa, wb = ma["stereoWidth"], mb["stereoWidth"]
    if wa is not None and wb is not None and abs(wa - wb) > 0.1:
        diffs.append(f"Your mix is {'wider' if wa > wb else 'narrower'} than the reference (width {wa} vs {wb}).")
    ta, tb = la["tempo"].get("bpm"), lb["tempo"].get("bpm")
    if ta and tb and abs(ta - tb) > 1:
        diffs.append(f"Tempo differs: about {ta} vs {tb} BPM.")
    ka, kb = la["key"].get("key"), lb["key"].get("key")
    if ka and kb and ka != kb:
        diffs.append(f"Key differs: {ka} vs {kb}.")
    ca, cb = _curve_norm(la["energyCurveDb"]), _curve_norm(lb["energyCurveDb"])
    ca_r, cb_r = ca - ca.max(), cb - cb.max()
    corr = float(np.corrcoef(ca, cb)[0, 1]) if ca.std() > 0 and cb.std() > 0 else None
    span_a, span_b = float(ca.max() - np.percentile(ca, 10)), float(cb.max() - np.percentile(cb, 10))
    if abs(span_a - span_b) >= 3:
        diffs.append(f"The reference has {'less' if span_a > span_b else 'more'} contrast between quiet and loud parts "
                     f"({span_b:.1f} vs your {span_a:.1f} dB).")
    if corr is not None and corr < 0.5:
        diffs.append("The energy shape over time is quite different from the reference (builds and drops fall in different places).")
    tia, tib = la["timbre"]["mix"], lb["timbre"]["mix"]
    if tia.get("centroidHz") and tib.get("centroidHz"):
        r = tia["centroidHz"] / tib["centroidHz"]
        if r > 1.25 or r < 0.8:
            diffs.append(f"Your mix sounds {'brighter' if r > 1 else 'darker'} overall (centroid {tia['centroidHz']} vs {tib['centroidHz']} Hz).")
    if not diffs:
        diffs.append("Very close to the reference on loudness, balance, width and energy shape.")
    return {"mix": {"lufs": ma["integratedLufs"], "truePeak": ma["truePeakDbtp"], "width": wa, "bpm": ta, "key": ka,
                    "crestDb": ma["crestDb"], "energyCurve16": [_r(v, 1) for v in ca_r]},
            "reference": {"lufs": mb["integratedLufs"], "truePeak": mb["truePeakDbtp"], "width": wb, "bpm": tb,
                          "key": kb, "crestDb": mb["crestDb"], "energyCurve16": [_r(v, 1) for v in cb_r]},
            "bandDiffDb": band_d, "energyShapeCorrelation": _r(corr, 3), "differences": diffs}


# ------------------------------------------------------------------ plugin hook
def run_plugins(x: np.ndarray, sr: int) -> dict:
    """PLAINDAW_LISTENERS='pkg.module:func,other:func' -> each func(x, sr) returns a dict."""
    spec = os.environ.get("PLAINDAW_LISTENERS", "").strip()
    out = {}
    for item in [s for s in spec.split(",") if s.strip()]:
        mod, _, fn = item.strip().partition(":")
        try:
            f = getattr(importlib.import_module(mod), fn or "listen")
            out[item.strip()] = f(x, sr)
        except Exception as e:  # a broken plugin must not break the report
            out[item.strip()] = {"error": str(e)}
    return out


def key_relation(heard: str, project: str) -> str | None:
    """Plain-English note when the heard key differs from the project key."""
    from . import theory as th
    try:
        a, b = th.parse_key(heard), th.parse_key(project)
    except Exception:
        return None
    if a == b:
        return None
    shared = len(set(th.scale_pcs(*a)) & set(th.scale_pcs(*b)))
    if shared == 7:
        how = "it uses the same notes (relative key), so this is about which chord feels like home"
    elif shared >= 6:
        how = f"they share {shared} of 7 notes, so the audio leans towards a neighbouring key"
    else:
        how = (f"they share only {shared} of 7 notes: either a part is out of key, or the detector is hearing "
               "another chord as home (try `listen --tracks` on the melodic parts)")
    return f"The project is set to {project} and the audio sounds like {heard}; {how}."


def report_text(rep: dict) -> str:
    lines = list(rep.get("summary", []))
    k = (rep.get("key") or {}).get("key")
    if k and rep.get("projectKey"):
        note = key_relation(k, rep["projectKey"])
        if note:
            rep["keyNote"] = note
            lines.insert(1 if lines else 0, note)
    if rep.get("sectionEnergy"):
        lines.append("Section energy: " + ", ".join(f"{s['name']} {s['rmsDb']} dB" for s in rep["sectionEnergy"]))
    if rep.get("energyCurveDb"):
        lines.append("Energy per bar (dB): " + " ".join(str(v) for v in rep["energyCurveDb"][:64]))
    ref = rep.get("reference")
    if ref:
        lines.append("")
        lines.append("Compared with the reference:")
        lines += ["  - " + d for d in ref["differences"]]
    return "\n".join(lines)
