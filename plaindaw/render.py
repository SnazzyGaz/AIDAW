"""Offline renderer: project JSON -> stereo mix (+ stems), deterministic.

Signal flow per track (in graph order):
  source (clips/instrument) + inputs from children/sends
  -> insert chain (built-ins or VST3/CLAP via the sandbox)
  -> fader (gain/pan, automatable) -> output bus, sends (pre/post).
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import dsp
from .graph import order
from .media import decode
from .model import ModelError
from .plugins.sandbox import PluginCrashed, run_job
from .timebase import TimeMap, note_number


@dataclass
class RenderResult:
    mix: np.ndarray
    sr: int
    stems: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    start_sec: float = 0.0
    plugin_states: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.mix.shape[1] / self.sr


class Clock:
    def __init__(self, tm: TimeMap, sr: int):
        self.tm, self.sr = tm, sr
        self._q = np.array(tm.tempo_qn + [tm.tempo_qn[-1] + 1e6])
        last_sec = tm.tempo_sec[-1] + 1e6 * 60.0 / tm.tempo_bpm[-1]
        self._s = np.array(tm.tempo_sec + [last_sec])

    def sample(self, pos) -> int:
        return int(round(self.tm.to_sec(pos) * self.sr))

    def sample_qn(self, qn: float) -> int:
        return int(round(self.tm.sec(qn) * self.sr))

    def beats(self, idx: np.ndarray) -> np.ndarray:
        return np.interp(np.asarray(idx) / self.sr, self._s, self._q)

    def bpm(self, idx: np.ndarray) -> np.ndarray:
        q = self.beats(idx)
        i = np.searchsorted(np.array(self.tm.tempo_qn), q, side="right") - 1
        return np.array(self.tm.tempo_bpm)[np.clip(i, 0, None)]


def _rng(seed: int, *parts) -> np.random.Generator:
    return np.random.default_rng([int(seed)] + [zlib.crc32(str(p).encode()) for p in parts])


def _ref_kind(ref: str) -> tuple[str, str]:
    fmt, _, name = str(ref).partition(":")
    return fmt, name


def clip_len_qn(doc: dict, tm: TimeMap, c: dict, project_dir: Path | None = None) -> float:
    """Length of a clip on the timeline in quarter notes."""
    at = tm.qn(c.get("at", 0))
    if "pattern" in c:
        return c.get("lengthBeats") or doc.get("patterns", {}).get(c["pattern"], {}).get("lengthBeats", 4) * c.get("repeat", 1)
    if c.get("lengthBeats"):
        return float(c["lengthBeats"])
    if project_dir is None:
        return 0.0
    try:
        from .media import info
        dur = info(Path(project_dir) / c["file"])["durationSec"] - float(c.get("offsetSec", 0.0))
    except Exception:
        return 0.0
    st = c.get("stretch") or {}
    mode, speed = st.get("mode", "off"), 1.0
    if mode == "tempo":
        speed = tm.bpm_at(at) / float(st["sourceBpm"])
    elif mode == "ratio":
        speed = float(st.get("ratio", 1.0))
    elif mode == "varispeed":
        speed = float(st.get("ratio", 1.0)) * 2 ** (float(c.get("pitch", 0.0)) / 12)
    end_sec = tm.sec(at) + max(0.0, dur) / speed
    return max(0.0, tm.qn_at_sec(end_sec) - at)


def song_end_qn(doc: dict, tm: TimeMap, project_dir: Path | None = None) -> float:
    end = 0.0
    for t in doc["tracks"]:
        for c in t.get("clips", []):
            end = max(end, tm.qn(c.get("at", 0)) + max(clip_len_qn(doc, tm, c, project_dir), 0.0001))
        for a in t.get("automation", []):
            for pt in a.get("points", []):
                end = max(end, tm.qn(pt[0]))
    for m in doc.get("markers", []):
        end = max(end, tm.qn(m["at"]))
    return end


class Renderer:
    def __init__(self, doc: dict, project_dir: Path, sr: int | None = None, sandbox: bool | None = None):
        self.doc = doc
        self.dir = Path(project_dir)
        s = doc.get("settings", {})
        self.sr = int(sr or s.get("sampleRate", 48000))
        self.seed = int(s.get("seed", 1))
        self.block = int(s.get("block", 512))
        self.tail = float(s.get("tailSec", 2.0))
        self.sandbox = s.get("pluginSandbox", True) if sandbox is None else sandbox
        self.tm = TimeMap(doc.get("transport", {}))
        self.clock = Clock(self.tm, self.sr)
        self.warnings: list[str] = []
        self.plugin_states: dict = {}

    # ------------------------------------------------------------ helpers
    def curve(self, points: list, n: int, curve: str = "linear") -> np.ndarray:
        xs = np.array([self.clock.sample(p[0]) for p in points], dtype=np.float64)
        ys = np.array([float(p[1]) for p in points], dtype=np.float64)
        o = np.argsort(xs, kind="stable"); xs, ys = xs[o], ys[o]
        idx = np.arange(n)
        if curve == "hold":
            i = np.clip(np.searchsorted(xs, idx, side="right") - 1, 0, None)
            return ys[i]
        return np.interp(idx, xs, ys)

    def automation(self, t: dict, n: int) -> dict[str, np.ndarray]:
        out = {}
        for a in t.get("automation", []):
            if a.get("points"):
                out[a["target"]] = self.curve(a["points"], n, a.get("curve", "linear"))
        return out

    def notes_for(self, t: dict) -> list[tuple[int, int, int, float]]:
        pats = self.doc.get("patterns", {})
        res = []
        for c in t.get("clips", []):
            if "pattern" not in c or c.get("mute"):
                continue
            p = pats.get(c["pattern"])
            if p is None:
                raise ModelError(f"missing pattern {c['pattern']}")
            L = float(p.get("lengthBeats", 4))
            reps = int(c.get("repeat", 1))
            at = self.tm.qn(c.get("at", 0))
            clip_len = float(c.get("lengthBeats") or L * reps)
            tr = int(c.get("transpose", 0))
            vscale = float(c.get("velocity", 100)) / 100.0
            for r in range(max(1, int(np.ceil(clip_len / L)))):
                for nt in p.get("notes", []):
                    rel = r * L + float(nt["t"])
                    if float(nt["t"]) >= L or rel >= clip_len - 1e-9:
                        continue
                    ln = min(float(nt["len"]), clip_len - rel)
                    k = note_number(nt["key"]) + tr
                    if not 0 <= k <= 127:
                        continue
                    s = self.clock.sample_qn(at + rel)
                    e = max(s + 1, self.clock.sample_qn(at + rel + ln))
                    v = float(np.clip(float(nt.get("vel", 100)) * vscale, 1, 127))
                    res.append((s, e, k, v))
        res.sort()
        return res

    def plugin_job(self, ref: str, spec: dict, n: int, audio, notes, curves: dict, prefix: str) -> np.ndarray:
        from .plugins.scan import resolve
        fmt, path, pid = resolve(ref, spec.get("path"))
        state = None
        if spec.get("state"):
            sp = self.dir / spec["state"]
            if sp.exists():
                state = sp.read_bytes()
            else:
                self.warnings.append(f"plugin state file missing: {spec['state']}")
        points = []
        for target, cv in curves.items():
            if target.startswith(prefix):
                pname = target[len(prefix):]
                for f in range(0, n, 256):
                    points.append((f, pname, float(cv[f])))
        points.sort(key=lambda x: x[0])
        job = {"format": fmt, "path": path, "plugin_id": pid, "state": state, "params": spec.get("params") or {},
               "sample_rate": self.sr, "block": self.block, "n_frames": n,
               "audio": None if audio is None else np.ascontiguousarray(audio, dtype=np.float32),
               "notes": notes, "param_points": points}
        try:
            res = run_job(job, sandbox=bool(self.sandbox))
        except PluginCrashed as e:
            self.warnings.append(f"plugin crashed, replaced with {'silence' if audio is None else 'bypass'}: {e}")
            return np.zeros((2, n)) if audio is None else np.array(audio, dtype=np.float64)
        self.plugin_states[spec.get("id") or ref] = res.get("state")
        out = res["audio"]
        if out.shape[1] < n:
            out = np.pad(out, ((0, 0), (0, n - out.shape[1])))
        return out[:, :n]

    # ------------------------------------------------------------ sources
    def instrument(self, t: dict, n: int, curves: dict) -> np.ndarray:
        inst = t.get("instrument") or {"plugin": "builtin:synth"}
        fmt, name = _ref_kind(inst.get("plugin", "builtin:synth"))
        notes = self.notes_for(t)
        if fmt in ("clap", "vst3"):
            return self.plugin_job(inst["plugin"], inst, n, None, notes, curves, "instrument/")
        if fmt == "sfz":
            from .sfz import SfzError, render_notes
            try:
                return render_notes(name, notes, n, self.sr, self.dir, inst.get("params"), self.warnings)
            except SfzError as e:
                self.warnings.append(f"{t['id']}: {e}; rendered silence")
                return np.zeros((2, n))
        if fmt != "builtin" or name not in dsp.INSTRUMENTS:
            raise ModelError(f"unknown instrument {inst.get('plugin')!r}")
        params = dict(dsp.PARAMS[name]); params.update(inst.get("params") or {})
        params = {k: v for k, v in params.items() if not k.startswith("_")}
        out = np.zeros((2, n))
        samples = {}
        for i, (s, e, k, v) in enumerate(notes):
            if s >= n:
                continue
            rng = _rng(self.seed, t["id"], i)
            if name == "synth":
                y = dsp.synth_note(params, k, v, e - s, self.sr, rng)
            elif name == "drums":
                y = dsp.drum_hit(k, v, self.sr, rng, params)
            else:
                zone = None
                for z in params.get("zones") or []:
                    if z.get("keyLo", 0) <= k <= z.get("keyHi", 127):
                        zone = z; break
                f = (zone or params).get("file")
                if not f:
                    raise ModelError(f"sampler on {t['id']} has no file")
                if f not in samples:
                    samples[f] = decode(self.dir / f, self.sr)
                zp = dict(params); zp.update(zone or {})
                y = dsp.sampler_note(samples[f], self.sr, k, v, e - s, zp)
            m = min(y.shape[1], n - s)
            out[:, s:s + m] += y[:, :m]
        return out

    def audio_clips(self, t: dict, n: int) -> np.ndarray:
        out = np.zeros((2, n))
        for c in t.get("clips", []):
            if "file" not in c or c.get("mute"):
                continue
            start = self.clock.sample(c.get("at", 0))
            if start >= n:
                continue
            data = np.array(decode(self.dir / c["file"], self.sr))
            if c.get("reverse"):
                data = data[:, ::-1]
            off = int(round(float(c.get("offsetSec", 0.0)) * self.sr))
            data = data[:, off:]
            st = c.get("stretch") or {}
            mode = st.get("mode", "off")
            pitch = float(c.get("pitch", 0.0))
            at_qn = self.tm.qn(c.get("at", 0))
            speed = 1.0
            if mode == "tempo":
                speed = self.tm.bpm_at(at_qn) / float(st["sourceBpm"])
            elif mode == "ratio":
                speed = float(st.get("ratio", 1.0))
            if mode == "varispeed":
                speed = float(st.get("ratio", 1.0)) * 2 ** (pitch / 12)
                m = data.shape[1]
                pos = np.arange(int(m / speed)) * speed
                data = np.vstack([np.interp(pos, np.arange(m), data[ch]) for ch in range(2)])
            elif abs(speed - 1.0) > 1e-9 or pitch:
                import pedalboard
                data = pedalboard.time_stretch(np.ascontiguousarray(data, dtype=np.float32), self.sr,
                                               stretch_factor=speed, pitch_shift_in_semitones=pitch).astype(np.float64)
            if c.get("lengthBeats"):
                L = self.clock.sample_qn(at_qn + float(c["lengthBeats"])) - start
                if data.shape[1] < L and c.get("loop") and data.shape[1] > 0:
                    data = np.tile(data, (1, int(np.ceil(L / data.shape[1]))))
                data = data[:, :L]
            m = data.shape[1]
            if m == 0:
                continue
            fi = int(float(c.get("fadeIn", 0.0)) * self.sr)
            fo = int(float(c.get("fadeOut", 0.0)) * self.sr)
            if fi > 0:
                fi = min(fi, m); data[:, :fi] *= np.sin(np.linspace(0, np.pi / 2, fi)) ** 2
            if fo > 0:
                fo = min(fo, m); data[:, m - fo:] *= np.cos(np.linspace(0, np.pi / 2, fo)) ** 2
            data *= dsp.db2lin(float(c.get("gainDb", 0.0)))
            k = min(m, n - start)
            out[:, start:start + k] += data[:, :k]
        return out

    # ------------------------------------------------------------ main
    def run(self, start_qn: float | None = None, end_qn: float | None = None, want_stems: bool = False,
            include_tail: bool = True) -> RenderResult:
        doc = self.doc
        end = song_end_qn(doc, self.tm, self.dir) if end_qn is None else end_qn
        n = self.clock.sample_qn(end) + (int(self.tail * self.sr) if include_tail else 0)
        n = max(n, self.sr // 10)
        by_id = {t["id"]: t for t in doc["tracks"]}
        inputs = {tid: np.zeros((2, n)) for tid in by_id}
        post: dict[str, np.ndarray] = {}
        soloed = {t["id"] for t in doc["tracks"] if t.get("mix", {}).get("solo")}
        audible = self._audible(soloed)
        for tid in order(doc):
            t = by_id[tid]
            curves = self.automation(t, n)
            typ = t["type"]
            x = inputs[tid]
            if typ == "instrument":
                x = x + self.instrument(t, n, curves)
            elif typ == "audio":
                x = x + self.audio_clips(t, n)
            for ins in t.get("inserts", []):
                if ins.get("bypass"):
                    continue
                x = self.insert(t, ins, x, n, curves, post)
            mix = t.get("mix", {})
            g = curves.get("mix/gainDb")
            g = dsp.db2lin(g) if g is not None else float(dsp.db2lin(mix.get("gainDb", 0.0)))
            pan = curves.get("mix/pan", mix.get("pan", 0.0))
            gl, gr = dsp.pan_gains(pan)
            silent = mix.get("mute") or (tid not in audible)
            y = np.zeros((2, n)) if silent else np.vstack([x[0] * g * gl, x[1] * g * gr])
            post[tid] = y
            if typ != "master":
                if not silent:
                    for s in t.get("sends", []):
                        lv = curves.get(f"sends/{s['to']}/levelDb")
                        lv = dsp.db2lin(lv) if lv is not None else float(dsp.db2lin(s.get("levelDb", -12.0)))
                        src = x if s.get("pre") else y
                        inputs[s["to"]] += src * lv
                inputs[t["output"]] += y
        master = next(t["id"] for t in doc["tracks"] if t["type"] == "master")
        mixed = post[master]
        s0 = 0 if start_qn is None else self.clock.sample_qn(start_qn)
        e0 = n if end_qn is None else (n if include_tail else self.clock.sample_qn(end_qn))
        res = RenderResult(mix=mixed[:, s0:e0], sr=self.sr, warnings=self.warnings, start_sec=s0 / self.sr,
                           plugin_states=self.plugin_states)
        if want_stems:
            res.stems = {tid: post[tid][:, s0:e0] for tid in post if tid != master}
        return res

    def _audible(self, soloed: set) -> set:
        """Solo rule: a track plays if no solo is active, or it is a return/master, or it
        or one of its parent buses is soloed, or it is a parent bus of a soloed track."""
        by_id = {t["id"]: t for t in self.doc["tracks"]}
        if not soloed:
            return set(by_id)

        def chain(tid):
            out, cur = [], by_id[tid]
            while cur["type"] != "master" and cur.get("output") in by_id and cur["id"] not in out:
                out.append(cur["id"]); cur = by_id[cur["output"]]
            return out

        aud = {tid for tid, t in by_id.items() if t["type"] in ("return", "master")}
        for tid in by_id:
            c = chain(tid)
            if any(x in soloed for x in c):
                aud.add(tid)
        for s in soloed:
            aud.update(chain(s))
        return aud

    def insert(self, t, ins, x, n, curves, post):
        fmt, name = _ref_kind(ins.get("plugin", ""))
        pid = ins.get("id")
        prefix = f"inserts/{pid}/"
        if fmt in ("clap", "vst3"):
            return self.plugin_job(ins["plugin"], ins, n, x, None, curves, prefix)
        if fmt != "builtin" or name not in dsp.FX:
            raise ModelError(f"unknown plugin {ins.get('plugin')!r} on {t['id']}")
        params = {k: v for k, v in dsp.PARAMS[name].items() if not k.startswith("_")}
        params.update(ins.get("params") or {})
        sc = None
        if ins.get("sidechain"):
            sc = post.get(ins["sidechain"])
            if sc is None:
                self.warnings.append(f"sidechain source {ins['sidechain']} not rendered before {t['id']}")
            params["_sidechain"] = sc is not None
        local = {k[len(prefix):]: v for k, v in curves.items() if k.startswith(prefix)}
        ctx = dsp.Ctx(self.sr, self.clock.bpm, n, local, sc, self.clock.beats, self.seed)
        y = dsp.FX[name](x, params, ctx)
        return y


def render(doc: dict, project_dir: Path, *, sr: int | None = None, start=None, end=None, stems: bool = False,
           include_tail: bool = True, sandbox: bool | None = None) -> RenderResult:
    r = Renderer(doc, project_dir, sr=sr, sandbox=sandbox)
    sq = None if start is None else r.tm.qn(start)
    eq = None if end is None else r.tm.qn(end)
    return r.run(sq, eq, stems, include_tail)
