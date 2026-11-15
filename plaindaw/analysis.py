"""Measurements so an AI (or a person) can "hear" a render as numbers.

Loudness follows ITU-R BS.1770-4 / EBU R128 (K-weighting, gating, LRA per EBU Tech 3342).
"""
from __future__ import annotations

import math

import numpy as np
from scipy import signal

from .dsp import true_peak_env

BANDS = [("sub", 20, 60), ("low", 60, 250), ("lowmid", 250, 500), ("mid", 500, 2000),
         ("highmid", 2000, 4000), ("presence", 4000, 8000), ("air", 8000, 20000)]

TARGETS = {
    "club": {"integratedLufs": (-9.0, 1.5), "truePeakMax": -1.0},
    "streaming": {"integratedLufs": (-14.0, 1.0), "truePeakMax": -1.0},
    "podcast": {"integratedLufs": (-16.0, 1.0), "truePeakMax": -1.0},
    "film": {"integratedLufs": (-24.0, 2.0), "truePeakMax": -2.0},
}


def _r(x, d=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), d)


def k_weight(x: np.ndarray, sr: float) -> np.ndarray:
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = math.tan(math.pi * f0 / sr); Vh = 10 ** (G / 20); Vb = Vh ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    b1 = [(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0]
    a1 = [1, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = math.tan(math.pi * f0 / sr)
    d = 1 + K / Q + K * K
    b2 = [1, -2, 1]
    a2 = [1, 2 * (K * K - 1) / d, (1 - K / Q + K * K) / d]
    sos = np.array([b1 + a1, b2 + a2])
    return signal.sosfilt(sos, x, axis=-1)


def _block_power(kw: np.ndarray, sr: float, win: float, hop: float) -> np.ndarray:
    W, H = int(round(win * sr)), int(round(hop * sr))
    n = kw.shape[-1]
    if n < W:
        return np.array([])
    sq = kw ** 2
    cs = np.concatenate([np.zeros((sq.shape[0], 1)), np.cumsum(sq, axis=1)], axis=1)
    starts = np.arange(0, n - W + 1, H)
    ms = (cs[:, starts + W] - cs[:, starts]) / W  # mean square per channel
    return ms.sum(axis=0)  # stereo channel weights are 1.0


def _lufs(p):
    return -0.691 + 10 * np.log10(np.maximum(p, 1e-20))


def loudness(x: np.ndarray, sr: float) -> dict:
    kw = k_weight(x, sr)
    mom = _block_power(kw, sr, 0.4, 0.1)
    st = _block_power(kw, sr, 3.0, 0.1)
    integ = None
    if mom.size:
        lm = _lufs(mom)
        g1 = mom[lm > -70]
        if g1.size:
            rel = _lufs(g1.mean()) - 10
            g2 = g1[_lufs(g1) > rel]
            if g2.size:
                integ = float(_lufs(g2.mean()))
    lra = None
    if st.size:
        ls = _lufs(st)
        g = ls[ls > -70]
        if g.size:
            rel = _lufs((10 ** ((g + 0.691) / 10)).mean()) - 20
            g = g[g > rel]
            if g.size > 1:
                lra = float(np.percentile(g, 95) - np.percentile(g, 10))
    def mx(p):
        if not p.size:
            return None
        v = float(_lufs(p).max())
        return _r(v) if v > -70 else None
    return {"integratedLufs": _r(integ), "momentaryMaxLufs": mx(mom), "shortTermMaxLufs": mx(st), "lra": _r(lra)}


def band_levels(x: np.ndarray, sr: float) -> dict:
    mono = x.mean(axis=0)
    if mono.size < 2048:
        mono = np.pad(mono, (0, 2048 - mono.size))
    f, p = signal.welch(mono, sr, nperseg=8192 if mono.size >= 8192 else 2048)
    total = np.trapezoid(p, f) + 1e-20
    out = {}
    for name, lo, hi in BANDS:
        m = (f >= lo) & (f < hi)
        e = np.trapezoid(p[m], f[m]) if m.sum() > 1 else 0.0
        out[f"{name}_{lo}_{hi}"] = _r(10 * math.log10(max(e, 1e-20) / total), 1)
    centroid = float((f * p).sum() / max(p.sum(), 1e-20))
    return {"bandsDbRel": out, "spectralCentroidHz": _r(centroid, 0)}


def _band_energy(x, sr):
    mono = x.mean(axis=0)
    if mono.size < 2048:
        mono = np.pad(mono, (0, 2048 - mono.size))
    f, p = signal.welch(mono, sr, nperseg=4096)
    return {name: float(np.trapezoid(p[(f >= lo) & (f < hi)], f[(f >= lo) & (f < hi)])) for name, lo, hi in BANDS}


def basic(x: np.ndarray, sr: float) -> dict:
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0
    tp = float(np.max(true_peak_env(x))) if x.size else 0.0
    L, R = x[0], x[1]
    denom = math.sqrt(float(np.sum(L * L) * np.sum(R * R))) or 1e-20
    corr = float(np.sum(L * R) / denom)
    mid, side = (L + R) / 2, (L - R) / 2
    ms, ss = float(np.mean(mid ** 2)), float(np.mean(side ** 2))
    width = math.sqrt(ss / ms) if ms > 1e-20 else 0.0
    stereo_rms = float(np.mean((L ** 2 + R ** 2) / 2))
    mono_loss = 10 * math.log10(max(ms, 1e-20) / max(stereo_rms, 1e-20)) if stereo_rms > 1e-20 else 0.0
    thr = 10 ** (-60 / 20)
    nz = np.where(np.max(np.abs(x), axis=0) > thr)[0]
    lead = nz[0] / sr if nz.size else x.shape[1] / sr
    trail = (x.shape[1] - 1 - nz[-1]) / sr if nz.size else 0.0
    clipped = int(np.sum(np.abs(x) >= 0.999))
    return {
        "samplePeakDbfs": _r(20 * math.log10(max(peak, 1e-12))),
        "truePeakDbtp": _r(20 * math.log10(max(tp, 1e-12))),
        "rmsDbfs": _r(20 * math.log10(max(rms, 1e-12))),
        "crestDb": _r(20 * math.log10(max(peak, 1e-12) / max(rms, 1e-12))),
        "stereoCorrelation": _r(corr, 3),
        "stereoWidth": _r(width, 3),
        "monoLossDb": _r(mono_loss),
        "dcOffset": _r(float(np.mean(x)), 6),
        "clippedSamples": clipped,
        "leadingSilenceSec": _r(lead, 3),
        "trailingSilenceSec": _r(trail, 3),
    }


def low_end_mono(x, sr) -> float | None:
    sos = signal.butter(4, 120, "lowpass", fs=sr, output="sos")
    lo = signal.sosfilt(sos, x, axis=-1)
    d = math.sqrt(float(np.sum(lo[0] ** 2) * np.sum(lo[1] ** 2)))
    return _r(float(np.sum(lo[0] * lo[1]) / d), 3) if d > 1e-12 else None


def analyze(x: np.ndarray, sr: int, *, stems: dict | None = None, sections: list | None = None,
            target: str | None = None, names: dict | None = None, no_masking: set | None = None) -> dict:
    """x: (2, n) mix. stems: {track id: (2, n)}. sections: [(name, start_sec, end_sec)]."""
    rep = {"_note": "measured from this render", "sampleRate": sr, "durationSec": _r(x.shape[1] / sr, 3)}
    m = {}
    m.update(loudness(x, sr))
    m.update(basic(x, sr))
    m["lowEndCorrelation"] = low_end_mono(x, sr)
    if m["integratedLufs"] is not None and m["truePeakDbtp"] is not None:
        m["plrDb"] = _r(m["truePeakDbtp"] - m["integratedLufs"])
    if m["integratedLufs"] is None:
        m["silent"] = True
    rep["master"] = m
    rep.update(band_levels(x, sr))
    warnings = []
    if m["truePeakDbtp"] is not None and m["truePeakDbtp"] > -1.0:
        warnings.append(f"true peak {m['truePeakDbtp']} dBTP is above -1 dBTP")
    if m["clippedSamples"]:
        warnings.append(f"{m['clippedSamples']} samples at or above full scale (clipping)")
    if m["stereoCorrelation"] is not None and m["stereoCorrelation"] < 0:
        warnings.append("negative stereo correlation: phase problems, will lose energy in mono")
    if m["lowEndCorrelation"] is not None and m["lowEndCorrelation"] < 0.8:
        warnings.append(f"low end (<120 Hz) is not mono (correlation {m['lowEndCorrelation']})")
    if abs(m["dcOffset"] or 0) > 0.001:
        warnings.append("DC offset present")
    if sections:
        secs = []
        for name, s, e in sections:
            a, b = int(s * sr), int(e * sr)
            seg = x[:, a:b]
            if seg.shape[1] < int(0.4 * sr):
                continue
            L = loudness(seg, sr)
            secs.append({"name": name, "startSec": _r(s, 3), "endSec": _r(e, 3), "integratedLufs": L["integratedLufs"],
                         "shortTermMaxLufs": L["shortTermMaxLufs"],
                         "peakDbfs": _r(20 * math.log10(max(float(np.max(np.abs(seg))), 1e-12)))})
        rep["sections"] = secs
        drops = [s for s in secs if "drop" in s["name"].lower() and s["integratedLufs"] is not None]
        builds = [s for s in secs if ("build" in s["name"].lower() or "break" in s["name"].lower()) and s["integratedLufs"] is not None]
        if drops and builds:
            contrast = max(d["integratedLufs"] for d in drops) - max(b["integratedLufs"] for b in builds)
            rep["dropContrastLu"] = _r(contrast)
            if contrast < 2:
                warnings.append(f"drop is only {contrast:.1f} LU louder than the build/break: weak impact")
    if stems:
        tr = {}
        energies = {}
        mix_e = _band_energy(x, sr)
        for tid, s in stems.items():
            pk = float(np.max(np.abs(s))) if s.size else 0.0
            if pk < 1e-7:
                tr[tid] = {"peakDbfs": None, "rmsDbfs": None, "silent": True}
                continue
            rms = float(np.sqrt(np.mean(s ** 2)))
            tr[tid] = {"peakDbfs": _r(20 * math.log10(pk)), "rmsDbfs": _r(20 * math.log10(max(rms, 1e-12))),
                       "integratedLufs": loudness(s, sr)["integratedLufs"]}
            energies[tid] = _band_energy(s, sr)
        rep["tracks"] = tr
        hints = []
        ids = sorted(k for k in energies if k not in (no_masking or set()))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = energies[ids[i]], energies[ids[j]]
                for band in ("sub", "low", "lowmid", "mid", "highmid"):
                    lo = next(x for x in BANDS if x[0] == band)
                    ea, eb, em = a[band], b[band], mix_e[band] + 1e-20
                    if ea <= 0 or eb <= 0:
                        continue
                    da, db_ = 10 * math.log10(ea / em), 10 * math.log10(eb / em)
                    if da > -8 and db_ > -8 and abs(da - db_) < 6:
                        hints.append({"tracks": [ids[i], ids[j]], "band": f"{lo[1]}-{lo[2]} Hz",
                                      "severity": _r(min(da, db_) + 8, 1)})
        hints.sort(key=lambda h: -h["severity"])
        rep["maskingHints"] = hints[:6]
        for h in hints[:3]:
            nm = names or {}
            warnings.append(f"{nm.get(h['tracks'][0], h['tracks'][0])} and {nm.get(h['tracks'][1], h['tracks'][1])} "
                            f"compete in {h['band']}")
    if target:
        t = TARGETS.get(target)
        if t is None:
            raise ValueError(f"unknown target {target!r} (choose {', '.join(TARGETS)})")
        res = []
        want, tol = t["integratedLufs"]
        il = m["integratedLufs"]
        if il is None:
            res.append({"check": "integratedLufs", "ok": False, "detail": "silent"})
        else:
            res.append({"check": "integratedLufs", "ok": abs(il - want) <= tol, "value": il, "target": want,
                        "detail": f"{il:+.1f} LUFS vs {want} ±{tol}"})
        tp = m["truePeakDbtp"]
        res.append({"check": "truePeak", "ok": tp is not None and tp <= t["truePeakMax"], "value": tp,
                    "target": t["truePeakMax"]})
        rep["target"] = {"profile": target, "checks": res, "pass": all(c["ok"] for c in res)}
    rep["warnings"] = warnings
    return rep


def compare(a: np.ndarray, b: np.ndarray, sr: int) -> dict:
    """A/B comparison, loudness matched."""
    la, lb = loudness(a, sr)["integratedLufs"], loudness(b, sr)["integratedLufs"]
    ba, bb = band_levels(a, sr)["bandsDbRel"], band_levels(b, sr)["bandsDbRel"]
    n = min(a.shape[1], b.shape[1])
    gain = 10 ** (((la or 0) - (lb or 0)) / 20) if la is not None and lb is not None else 1.0
    resid = a[:, :n] - b[:, :n] * gain
    rl = loudness(resid, sr)["integratedLufs"] if n else None
    return {"aLufs": la, "bLufs": lb, "deltaLu": _r((lb or 0) - (la or 0)) if la is not None and lb is not None else None,
            "bandDeltaDb": {k: _r((bb[k] or 0) - (ba[k] or 0), 1) for k in ba},
            "nullResidualLufs": rl,
            "identical": bool(n and a.shape == b.shape and np.array_equal(a, b))}
