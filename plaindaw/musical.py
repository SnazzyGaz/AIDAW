"""Musical commands: compose, arrange, fix clash, variations, humanize, preview.

Each command *plans* a list of ordinary ops (track.add, pattern.add, clip.add_pattern,
plugin.add, ...) and applies them as ONE batch, so it is one undo step, it is written to the
history with a plain-English `reason`, and locks/permissions apply exactly as for hand edits.
Everything is seeded and deterministic for the built-in instruments.
"""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

import numpy as np

from . import theory as th
from .ops import Ctx, OpError, apply_op
from .timebase import TimeMap

ROLE_NAMES = {"kick": "Kick", "clap": "Clap", "snare": "Snare", "hat": "Hats", "bass": "Bass", "chords": "Chords",
              "pad": "Pad", "lead": "Lead", "arp": "Arp", "roll": "Snare Roll", "riser": "Riser", "crash": "Crash"}
ROLE_GAIN = {"kick": -1.0, "clap": -5.0, "snare": -5.0, "hat": -9.0, "bass": -2.0, "chords": -9.0, "pad": -14.0,
             "lead": -9.0, "arp": -13.0, "roll": -8.0, "riser": -12.0, "crash": -9.0}
NAME_HINTS = [("kick", "kick"), ("bd", "kick"), ("clap", "clap"), ("snare roll", "roll"), ("roll", "roll"),
              ("snare", "snare"), ("hat", "hat"), ("hh", "hat"), ("perc", "hat"), ("bass", "bass"), ("808", "bass"),
              ("chord", "chords"), ("stab", "chords"), ("pad", "pad"), ("string", "pad"), ("lead", "lead"),
              ("hook", "lead"), ("melody", "lead"), ("arp", "arp"), ("riser", "riser"), ("sweep", "riser"),
              ("crash", "crash"), ("cymbal", "crash")]
# arp follows lead, snare follows clap in the section tables
EXTRA_ROLES = {"arp": "lead", "snare": "clap"}


class MusicalError(OpError):
    pass


# ------------------------------------------------------------------ built-in sound presets
SYNTH_PRESETS = {
    ("bass", "offbeat"): "wave=saw voices=2 detune=8 cutoff=420 resonance=0.15 envAmount=2.2 filterDecay=0.12 attack=0.002 decay=0.15 sustain=0.6 release=0.06 sub=0.6 gainDb=-4 width=0.2",
    ("bass", "rolling"): "wave=square voices=1 cutoff=650 resonance=0.25 envAmount=2.5 filterDecay=0.08 attack=0.001 decay=0.12 sustain=0.3 release=0.05 sub=0.4 gainDb=-5 width=0.1",
    ("bass", "reese"): "wave=saw voices=3 detune=22 cutoff=700 resonance=0.1 attack=0.01 decay=0.3 sustain=0.9 release=0.12 sub=0.5 gainDb=-6 width=0.5",
    ("bass", "808"): "wave=sine voices=1 cutoff=2000 attack=0.001 decay=1.2 sustain=0.6 release=0.25 pitchEnv=7 pitchDecay=0.04 glide=0.04 gainDb=-2 width=0",
    ("bass", "root"): "wave=triangle voices=1 cutoff=900 envAmount=1.0 filterDecay=0.2 attack=0.003 decay=0.4 sustain=0.5 release=0.1 sub=0.5 gainDb=-3 width=0",
    ("bass", "drone"): "wave=saw voices=2 detune=6 cutoff=300 attack=1.2 decay=0.5 sustain=1 release=2 sub=0.6 gainDb=-8 width=0.3",
    ("chords", "supersaw"): "wave=saw voices=7 detune=28 cutoff=5200 resonance=0.1 attack=0.01 decay=0.3 sustain=0.8 release=0.25 gainDb=-14 width=1.0",
    ("chords", "stabs"): "wave=saw voices=3 detune=14 cutoff=2600 resonance=0.2 envAmount=1.5 filterDecay=0.15 attack=0.002 decay=0.22 sustain=0.0 release=0.12 gainDb=-11 width=0.8",
    ("chords", "pad"): "wave=saw voices=5 detune=20 cutoff=2200 attack=0.5 decay=0.6 sustain=0.9 release=1.4 gainDb=-16 width=1.0",
    ("chords", "keys"): "wave=triangle voices=2 detune=4 cutoff=3500 envAmount=1.0 filterDecay=0.4 attack=0.004 decay=0.9 sustain=0.25 release=0.4 gainDb=-10 width=0.6",
    ("pad", "*"): "wave=saw voices=5 detune=18 cutoff=1800 attack=0.6 decay=0.5 sustain=0.9 release=1.5 gainDb=-17 width=1.0",
    ("lead", "anthem"): "wave=saw voices=5 detune=16 cutoff=5200 resonance=0.15 envAmount=1.0 filterDecay=0.25 attack=0.004 decay=0.3 sustain=0.7 release=0.2 gainDb=-14 width=0.7",
    ("lead", "pluck"): "wave=square voices=2 detune=8 cutoff=1800 resonance=0.2 envAmount=2.4 filterDecay=0.12 attack=0.001 decay=0.25 sustain=0.0 release=0.15 gainDb=-11 width=0.6",
    ("lead", "arp"): "wave=square voices=1 cutoff=2400 resonance=0.3 envAmount=2.0 filterDecay=0.08 attack=0.001 decay=0.15 sustain=0.0 release=0.08 gainDb=-12 width=0.4",
    ("lead", "bell"): "wave=sine voices=2 detune=3 cutoff=8000 attack=0.001 decay=1.1 sustain=0.0 release=0.6 gainDb=-9 width=0.5",
    ("lead", "sparse"): "wave=triangle voices=2 detune=5 cutoff=2500 attack=0.25 decay=0.8 sustain=0.6 release=1.6 gainDb=-12 width=0.8",
    ("arp", "*"): "wave=saw voices=2 detune=10 cutoff=2000 resonance=0.25 envAmount=2.0 filterDecay=0.1 attack=0.001 decay=0.18 sustain=0.0 release=0.1 gainDb=-14 width=0.9",
    ("riser", "*"): "wave=noise attack=3 decay=0.1 sustain=1 release=0.3 gainDb=-14 width=1",
}
DRUM_PRESETS = {"kick": "kickPitch=48 kickDecay=0.42 gainDb=-1", "clap": "gainDb=-6", "snare": "gainDb=-6 snareDecay=0.2",
                "hat": "gainDb=-11 hatDecay=0.045 openHatDecay=0.22", "roll": "gainDb=-8 snareDecay=0.15",
                "crash": "gainDb=-9 crashDecay=2.2"}
TRAP_KICK = "kickPitch=44 kickDecay=0.3 gainDb=-1"


def _kv(text: str) -> dict:
    out = {}
    for tok in text.split():
        k, _, v = tok.partition("=")
        try:
            out[k] = int(v) if re.fullmatch(r"-?\d+", v) else float(v)
        except ValueError:
            out[k] = {"true": True, "false": False}.get(v, v)
    return out


def synth_preset(role: str, style: str) -> dict:
    return _kv(SYNTH_PRESETS.get((role, style)) or SYNTH_PRESETS.get((role, "*")) or SYNTH_PRESETS[("lead", "pluck")])


# ------------------------------------------------------------------ helpers
def _ctx(doc):
    tm = TimeMap(doc["transport"])
    sig = tm._sig_for_qn(0.0)
    return tm, float(sig.qn_per_bar), tm.bpm_at(0.0)


def _key(doc, p, g):
    text = p.get("key") or doc["transport"].get("keySig") or g["key"]
    try:
        return th.parse_key(text) + (text,)
    except Exception as e:
        raise MusicalError(f"bad key {text!r}: {e}")


def _progression(p, g, tonic, scale):
    text = p.get("chords") or g["progression"]
    try:
        return th.parse_progression(text, tonic, scale)
    except Exception as e:
        raise MusicalError(f"bad chords {text!r}: {e}")


def _per_bar(chords, bars, chord_bars=1):
    return [chords[(b // max(1, chord_bars)) % len(chords)] for b in range(bars)]


def _bass_note(pc: int, lo: int = 33) -> int:
    return lo + ((pc - lo) % 12)


def _note(t, ln, k, v):
    return {"t": round(float(t), 6), "len": round(max(0.01, float(ln)), 6), "key": int(k), "vel": int(np.clip(round(v), 1, 127))}


def _find_track(doc, ref):
    if not ref:
        return None
    r = str(ref).lower()
    for t in doc["tracks"]:
        if t["id"].lower() == r or t["name"].lower() == r:
            return t
    return None


def track_role(t) -> str | None:
    if t.get("role"):
        return t["role"]
    if t["type"] not in ("instrument", "audio"):
        return None
    n = t["name"].lower()
    for hint, role in NAME_HINTS:
        if hint in n:
            return role
    return None


def _unique(existing: set, base: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)[:56] or "x"
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def _ids(doc):
    from .model import all_ids
    return set(all_ids(doc)) | set(doc.get("patterns", {}))


def _bar_pos(bar: int) -> str:
    return f"{int(bar)}.1.1"


def _qn_to_pos(tm, bar_qn, qn) -> str:
    bar = int(qn // bar_qn) + 1
    rem = qn - (bar - 1) * bar_qn
    beat = int(rem // 1) + 1
    six = int(round((rem - (beat - 1)) * 4)) + 1
    return f"{bar}.{beat}.{min(4, six)}"


def _symbols(chords):
    return [th.chord_symbol(r, q) for r, _, q in chords]


def _four_on_floor(g):
    return sorted(g["drums"].get("kick", [])) == [0, 4, 8, 12]


# ------------------------------------------------------------------ generators (pure, seeded)
def gen_bass(cps, style, bar_qn, rng, g):
    notes = []
    beats = int(round(bar_qn))
    for b, (root, ivs, q) in enumerate(cps):
        o = b * bar_qn
        r = _bass_note(root)
        fifth = r + 7 if r + 7 <= 47 else r - 5
        if style == "offbeat":
            for i in range(beats):
                notes.append(_note(o + i + 0.5, 0.4, r, 110))
        elif style == "rolling":
            for st in range(int(bar_qn * 4)):
                if st % 4 == 0:
                    continue
                k = r + 12 if rng.random() < 0.12 else r
                notes.append(_note(o + st * 0.25, 0.22, k, 96 + 18 * (st % 4 == 2)))
        elif style in ("reese", "drone"):
            k = r if style == "reese" else _bass_note(root, 28)
            if notes and notes[-1]["key"] == k and abs(notes[-1]["t"] + notes[-1]["len"] - o) < 0.1:
                notes[-1]["len"] = round(notes[-1]["len"] + bar_qn, 6)
            else:
                notes.append(_note(o, bar_qn - 0.05, k, 100 if style == "reese" else 90))
        elif style == "808":
            hits = sorted(g["drums"].get("kick") or [0, 7, 10])
            for i, st in enumerate(hits):
                end = hits[i + 1] if i + 1 < len(hits) else bar_qn * 4
                k = r
                if i == len(hits) - 1 and rng.random() < 0.35:
                    k = r + 12 if rng.random() < 0.5 else fifth
                notes.append(_note(o + st * 0.25, (end - st) * 0.25 - 0.05, k, 115))
        else:  # "root": hip-hop / lo-fi line
            rhythms = [[(0, 1.5, 0), (1.75, 0.25, 0), (2.5, 1.0, 1)], [(0, 2.0, 0), (2.5, 0.5, 0), (3.0, 1.0, 2)],
                       [(0, 1.0, 0), (1.5, 0.5, 0), (2.0, 1.5, 1), (3.5, 0.5, 2)]]
            for t, ln, which in rhythms[int(rng.integers(len(rhythms)))]:
                if t < bar_qn:
                    k = [r, fifth, r + 12 if r + 12 <= 50 else r][which]
                    notes.append(_note(o + t, ln, k, 104 - 8 * which))
    return notes


def gen_chords(cps, style, bar_qn, rng, center=60, voices=4):
    vo = th.voice_lead(cps, center=center, voices=voices)
    notes = []
    stabs = [[0.5, 1.5, 2.5, 3.5], [0.75, 1.5, 2.75, 3.5], [0.5, 1.25, 2.5, 3.25]][int(rng.integers(3))]
    keys = [[(0, 1.5), (1.5, 0.5), (2.5, 1.5)], [(0, 2.0), (2.5, 1.0), (3.5, 0.5)], [(0, 3.5)]][int(rng.integers(3))]
    for b, chord in enumerate(vo):
        o = b * bar_qn
        if style == "stabs":
            for t in stabs:
                if t < bar_qn:
                    notes += [_note(o + t, 0.2, k, 100) for k in chord]
        elif style == "keys":
            for t, ln in keys:
                if t < bar_qn:
                    v = 88 + float(rng.uniform(-6, 6))
                    notes += [_note(o + t + 0.01 * i, ln, k, v - 4 * i) for i, k in enumerate(chord)]
        elif style == "pad":
            notes += [_note(o, bar_qn, k, 82) for k in chord]
        else:  # supersaw, sustained with a breath before the next chord
            notes += [_note(o, bar_qn - 0.5, k, 96) for k in chord]
    return notes


def gen_arp(cps, bar_qn, rng, center=64, rate=0.25):
    vo = th.voice_lead(cps, center=center, voices=3)
    shape = ["up", "updown", "down"][int(rng.integers(3))]
    notes = []
    for b, chord in enumerate(vo):
        seq = sorted(chord) + [k + 12 for k in sorted(chord)]
        if shape == "down":
            seq = seq[::-1]
        elif shape == "updown":
            seq = seq + seq[-2:0:-1]
        for i in range(int(round(bar_qn / rate))):
            notes.append(_note(b * bar_qn + i * rate, rate * 0.8, seq[i % len(seq)], 92 + 14 * (i % 4 == 0)))
    return notes


LEAD_CELLS = {  # onsets within one beat, and how much of the gap a note fills
    "anthem": ([[0, 0.75], [0, 0.5], [0], [0, 0.5, 0.75], [0.5], [0.25, 0.75], [0, 0.5]], 0.9),
    "pluck": ([[0, 0.5, 0.75], [0.25, 0.75], [0, 0.25, 0.5], [0.5, 0.75], [0, 0.75]], 0.22),
    "bell": ([[0], [0, 0.5], [0.5], [], [0]], 0.95),
    "sparse": ([[0], [], [], [0.5]], 0.95),
}


def _lead_rhythm(rng, style, qn_len):
    cells, fill = LEAD_CELLS.get(style, LEAD_CELLS["anthem"])
    on = [beat + x for beat in range(int(qn_len)) for x in cells[int(rng.integers(len(cells)))]] or [0.0]
    out = []
    for i, t in enumerate(on):
        gap = (on[i + 1] if i + 1 < len(on) else qn_len) - t
        ln = gap * fill if fill > 0.5 else min(gap * 0.9, 0.45)
        out.append((t, max(0.1, ln)))
    return out


def gen_lead(cps, style, bar_qn, rng, tonic, scale, center=72):
    """Motif-based melody: phrase A, A refitted to the next chords, A again, then a new answer
    phrase that ends on the tonic. Strong beats prefer chord tones, motion prefers steps."""
    if style == "arp":
        return gen_arp(cps, bar_qn, rng, center=center)
    pcs = set(th.scale_pcs(tonic, scale))
    pool = [k for k in range(center - 10, center + 15) if k % 12 in pcs]
    bars = len(cps)
    phrase_bars = 2 if bars >= 2 else 1
    plen = phrase_bars * bar_qn
    steps_w = {0: 0.6, 1: 3.0, 2: 3.0, 3: 2.0, 4: 2.0, 5: 1.0, 7: 0.8}

    def chord_tones(b):
        root, ivs, _ = cps[b % bars]
        return {(root + i) % 12 for i in ivs}

    def walk(rhythm, start_bar, prev, end_on_tonic=False):
        out = []
        for i, (t, ln) in enumerate(rhythm):
            b = start_bar + int(t // bar_qn)
            if b >= bars:
                break
            strong = abs(t - round(t)) < 1e-6
            ct = chord_tones(b)
            w = []
            for k in pool:
                d = abs(k - prev)
                wt = steps_w.get(d, 0.15 if d <= 12 else 0.0)
                wt *= (3.0 if k % 12 in ct else 0.4) if strong else 1.0
                w.append(wt)
            w = np.array(w)
            w = w if w.sum() > 0 else np.ones(len(pool))
            k = pool[int(rng.choice(len(pool), p=w / w.sum()))]
            if end_on_tonic and i == len(rhythm) - 1:
                k = min((x for x in pool if x % 12 == tonic), key=lambda x: abs(x - prev))
            out.append(_note(start_bar * bar_qn + t, ln, k, 100 + 10 * strong + float(rng.uniform(-6, 6))))
            prev = k
        return out, prev

    def refit(motif, dst_bar):
        out = []
        for n in motif:
            t = n["t"]
            b = dst_bar + int(t // bar_qn)
            if b >= bars:
                break
            k = n["key"]
            ct = chord_tones(b)
            if abs(t - round(t)) < 1e-6 and k % 12 not in ct:
                k = min((x for x in pool if x % 12 in ct), key=lambda x: (abs(x - k), x))
            out.append(dict(n, t=round(dst_bar * bar_qn + t, 6), key=k))
        return out

    start = min(pool, key=lambda x: (x % 12 not in chord_tones(0), abs(x - center)))
    a, prev = walk(_lead_rhythm(rng, style, plen), 0, start)
    notes = list(a)
    b, k = phrase_bars, 1
    while b < bars:
        last = b + phrase_bars >= bars
        if k % 4 == 3 or (last and k > 1):
            seg, prev = walk(_lead_rhythm(rng, style, plen), b, prev, end_on_tonic=last)
        else:
            seg = refit(a, b)
        notes += seg
        b += phrase_bars
        k += 1
    return notes


def gen_drum_voice(voice, g, bars, bar_qn, rng, fills=True):
    steps = g["drums"].get(voice) or []
    key = th.GM[voice]
    notes = []
    for b in range(bars):
        o = b * bar_qn
        for st in steps:
            if st * 0.25 >= bar_qn:
                continue
            if voice == "hat":
                v, ln = (98 if st % 4 == 2 else 72) + float(rng.uniform(-8, 8)), 0.125
            elif voice in ("shaker", "ride"):
                v, ln = (80 if st % 4 == 2 else 58) + float(rng.uniform(-6, 6)), 0.125
            elif voice == "openhat":
                v, ln = 84, 0.25
            elif voice == "kick":
                v, ln = 120, 0.25
            else:
                v, ln = 112, 0.25
            notes.append(_note(o + st * 0.25, ln, key, v))
        end_of_phrase = (b % 4 == 3) or b == bars - 1
        if fills and voice in ("clap", "snare") and end_of_phrase and bars >= 4 and rng.random() < 0.75:
            fill = [13, 14, 15] if rng.random() < 0.5 else [14, 15]
            for j, st in enumerate(fill):
                if not any(abs(n["t"] - (o + st * 0.25)) < 1e-6 for n in notes):
                    notes.append(_note(o + st * 0.25, 0.2, key, 70 + 15 * j))
    return notes


# ------------------------------------------------------------------ compose
COMPOSE_PARTS = ("bass", "chords", "pad", "lead", "arp", "drums", "song")
PART_ALIASES = {"chord": "chords", "melody": "lead", "hook": "lead", "beat": "drums", "drum": "drums",
                "bassline": "bass", "all": "song", "strings": "pad", "track": "song"}


def _is_genre(x) -> bool:
    x = str(x or "").lower()
    return x in th.GENRES or x in th.GENRE_ALIASES


def _kit():
    try:
        from . import kit
        return kit
    except Exception:
        return None


def _instrument_spec(role, style, engine, preset=None, project_dir=None):
    """(plugin, params, path, state, label) for a role: Surge XT from the kit when installed
    (engine auto|surge), else the deterministic built-ins."""
    if role in ("kick", "clap", "snare", "hat", "roll", "crash"):
        return "builtin:drums", _kv(DRUM_PRESETS.get(role, "gainDb=-6")), None, None, "built-in drums"
    if engine == "sfz":
        k = _kit()
        which = "piano" if style in ("keys", "bell", "pluck") else "strings"
        if k is None or k.sfz_path(which) is None:
            raise MusicalError(f"SFZ set {which!r} is not installed; run `plaindaw kit install`")
        return f"sfz:{which}", {}, None, None, f"SFZ {which} ({'sfizz' if k.installed()['sfizz'] else 'built-in player'})"
    if engine in ("auto", "surge", "kit"):
        k = _kit()
        pick = k.surge_preset_for(role, style, preset, project_dir) if k is not None else None
        if pick:
            return pick["plugin"], {}, pick.get("path"), pick.get("state"), f"Surge XT: {pick['name']}"
        if engine in ("surge", "kit"):
            raise MusicalError("Surge XT is not installed; run `plaindaw kit install` or use --engine builtin")
    return "builtin:synth", synth_preset(role, style), None, None, f"built-in synth ({style})"


def _ensure_track(doc, ops, ids, role, name, style, engine, preset=None, plugin=None, project_dir=None):
    """Find or plan a track for `role`; returns (track_id, created, sound_label)."""
    t = _find_track(doc, name) if name else None
    if t is None and not name:
        t = next((x for x in doc["tracks"] if track_role(x) == role and x["type"] == "instrument"), None)
    if t is not None:
        if t["type"] != "instrument":
            raise MusicalError(f"track {t['name']!r} is not an instrument track")
        if not t.get("role"):
            ops.append({"op": "track.set", "args": {"track": t["id"], "role": role}})
        return t["id"], False, "existing track"
    nm = name or ROLE_NAMES.get(role, role.title())
    tid = _unique(ids, nm.lower().replace(" ", "_"))
    ids.add(tid)
    if plugin:
        ref, params, path, state, label = plugin, {}, None, None, plugin
    else:
        ref, params, path, state, label = _instrument_spec(role, style, engine, preset, project_dir)
    args = {"name": nm, "id": tid, "plugin": ref, "role": role}
    if params:
        args["params"] = params
    if path:
        args["path"] = path
    if state:
        args["state"] = state
    ops.append({"op": "track.add", "args": args})
    ops.append({"op": "mix.set", "args": {"track": tid, "gainDb": ROLE_GAIN.get(role, -8.0)}})
    return tid, True, label


def _return_for(doc, ops, ids, created):
    for t in doc["tracks"]:
        if t["type"] == "return" and ("verb" in t["id"].lower() or "verb" in t["name"].lower()):
            return t["id"]
    if created:
        return created[0]
    rid = _unique(ids, "verb")
    ids.add(rid)
    ops.append({"op": "track.add", "args": {"name": "Verb", "id": rid, "type": "return"}})
    ops.append({"op": "plugin.add", "args": {"track": rid, "plugin": "builtin:reverb", "id": f"{rid}_reverb",
                                             "params": {"size": 0.85, "damping": 0.4, "mix": 1.0, "lowCutHz": 250, "predelayMs": 20}}})
    created.append(rid)
    return rid


def plan_compose(s, p: dict, doc: dict | None = None) -> dict:
    """Plan a compose request: returns {ops, reason, info, label} without applying anything."""
    doc = copy.deepcopy(doc if doc is not None else s.doc)
    part = str(p.get("part") or "song").lower()
    part = PART_ALIASES.get(part, part)
    if part not in COMPOSE_PARTS:
        raise MusicalError(f"compose what? choose {', '.join(COMPOSE_PARTS)} (got {part!r})")
    gname = p.get("genre") or (p.get("style") if _is_genre(p.get("style")) else None) or doc.get("meta", {}).get("genre")
    try:
        g = th.genre(gname)
    except th.TheoryError as e:
        raise MusicalError(str(e))
    part_style = p.get("style") if p.get("style") and not _is_genre(p.get("style")) else None
    seed = int(p.get("seed", 1))
    engine = str(p.get("engine", "auto")).lower()
    ops: list = []
    ids = _ids(doc)
    info = {"part": part, "genre": g["name"], "seed": seed, "tracks": [], "patterns": [], "sounds": {}}
    if part == "song":
        bpm = float(p.get("bpm") or p.get("tempo") or g["bpm"])
        ops.append({"op": "tempo.set", "args": {"bpm": bpm}})
        info["bpm"] = bpm
    tonic, scale, _ = _key(doc, p, g)
    if part == "song" or p.get("key") or not doc["transport"].get("keySig"):
        ops.append({"op": "keysig.set", "args": {"key": th.key_name(tonic, scale)}})
    chords = _progression(p, g, tonic, scale)
    tm, bar_qn, _ = _ctx(doc)
    # an arranged track (pattern clips spread over the song) gets its pattern swapped in every clip
    swap = None
    if part != "song" and p.get("at") is None and doc.get("markers"):
        roles = {"drums": ("kick", "clap", "snare", "hat")}.get(part, (part,))
        for t in doc["tracks"]:
            pcs = [c for c in t.get("clips", []) if c.get("pattern")]
            if track_role(t) in roles and len(pcs) >= 2:
                swap = True
                if not p.get("bars"):
                    pl = doc["patterns"].get(pcs[0]["pattern"], {}).get("lengthBeats", 8 * bar_qn)
                    p = {**p, "bars": max(1, int(round(pl / bar_qn)))}
                break
    bars = int(p.get("bars") or 8)
    if not 1 <= bars <= 256:
        raise MusicalError("bars must be 1..256")
    cps = _per_bar(chords, bars, int(p.get("chordBars", 1)))
    at = str(p.get("at") or "1.1.1")
    try:
        at_qn = tm.qn(at)
    except Exception as e:
        raise MusicalError(f"bad position {at!r}: {e}")
    L = bars * bar_qn
    info.update({"key": th.key_name(tonic, scale), "chords": _symbols(chords), "bars": bars, "at": at})
    created_returns: list = []
    swing = float(p.get("swing", g.get("swing", 0.0)))
    from .render import clip_len_qn

    def add_part(role, notes, style, name=None, preset=None):
        if not notes:
            return None
        tid, created, label = _ensure_track(doc, ops, ids, role, name, style, engine, preset,
                                            p.get("instrument") if part != "song" else None, s.dir)
        t = _find_track(doc, tid)
        placed = [c for c in (t or {}).get("clips", []) if c.get("pattern")]
        if t is not None and swap and len(placed) >= 2:
            pid = _unique(ids, f"{tid}_{style}_s{seed}")
            ids.add(pid)
            ops.append({"op": "pattern.add", "args": {"id": pid, "name": f"{ROLE_NAMES.get(role, role)} ({style}, seed {seed})",
                                                      "lengthBeats": L, "notes": notes}})
            if swing > 0 and role not in ("pad", "riser", "crash"):
                ops.append({"op": "pattern.swing", "args": {"pattern": pid, "amount": round(swing, 3), "grid": 0.25}})
            for c in placed:
                ops.append({"op": "clip.remove", "args": {"track": tid, "clip": c["id"]}})
                args = {"track": tid, "pattern": pid, "at": c["at"], "id": c["id"]}
                for k in ("lengthBeats", "repeat", "velocity", "transpose"):
                    if k in c:
                        args[k] = c[k]
                ops.append({"op": "clip.add_pattern", "args": args})
            info["tracks"].append(tid)
            info["patterns"].append(pid)
            info["sounds"][tid] = label
            info["replacedClips"] = info.get("replacedClips", 0) + len(placed)
            return tid, created
        if t is not None:  # recomposing: replace clips in the target range
            for c in t.get("clips", []):
                c0 = tm.qn(c["at"])
                c1 = c0 + clip_len_qn(doc, tm, c)
                if c0 < at_qn + L - 1e-6 and c1 > at_qn + 1e-6:
                    ops.append({"op": "clip.remove", "args": {"track": tid, "clip": c["id"]}})
        pid = _unique(ids, f"{tid}_{style}_s{seed}")
        ids.add(pid)
        ops.append({"op": "pattern.add", "args": {"id": pid, "name": f"{ROLE_NAMES.get(role, role)} ({style}, seed {seed})",
                                                  "lengthBeats": L, "notes": notes}})
        if swing > 0 and role not in ("pad", "riser", "crash"):
            ops.append({"op": "pattern.swing", "args": {"pattern": pid, "amount": round(swing, 3), "grid": 0.25}})
        cid = _unique(ids, f"{tid}_c{seed}")
        ids.add(cid)
        ops.append({"op": "clip.add_pattern", "args": {"track": tid, "pattern": pid, "at": at, "id": cid}})
        info["tracks"].append(tid)
        info["patterns"].append(pid)
        info["sounds"][tid] = label
        return tid, created

    def rng(tag):
        return np.random.default_rng([seed, sum(ord(ch) * (i + 1) for i, ch in enumerate(tag))])

    want = [part] if part != "song" else ["drums", "bass", "chords", "pad", "lead"]
    style_for = {"bass": p.get("bassStyle") or (part_style if part == "bass" else None) or g["bass"],
                 "chords": p.get("chordStyle") or (part_style if part == "chords" else None) or g["chords"],
                 "lead": p.get("leadStyle") or (part_style if part == "lead" else None) or g["lead"]}
    target = p.get("track") if part != "song" else None
    made = {}
    if "drums" in want:
        groups = {"kick": ["kick"], "clap": ["clap"], "snare": ["snare"], "hat": ["hat", "openhat", "shaker", "ride"]}
        for role, vs in groups.items():
            ns = []
            for v in vs:
                if g["drums"].get(v):
                    ns += gen_drum_voice(v, g, bars, bar_qn, rng(v))
            if not ns:
                continue
            ns.sort(key=lambda n: (n["t"], n["key"]))
            r = add_part(role, ns, g["name"])
            made[role] = r[0]
            if r[1] and role == "kick" and g["name"] in ("trap", "hiphop", "lofi"):
                ops.append({"op": "instrument.set", "args": {"track": r[0], "plugin": "builtin:drums", "params": _kv(TRAP_KICK)}})
            if r[1] and role == "hat":
                ops.append({"op": "plugin.add", "args": {"track": r[0], "plugin": "builtin:eq", "id": f"{r[0]}_eq",
                                                         "params": {"lowCutHz": 500, "highShelfDb": 2}}})
                ops.append({"op": "mix.set", "args": {"track": r[0], "pan": 0.15}})
    kick_id = made.get("kick") or next((t["id"] for t in doc["tracks"] if track_role(t) == "kick"), None)
    if "bass" in want:
        st = style_for["bass"]
        r = add_part("bass", gen_bass(cps, st, bar_qn, rng("bass"), g), st, target, p.get("preset"))
        if r and r[1]:
            ops.append({"op": "plugin.add", "args": {"track": r[0], "plugin": "builtin:utility", "id": f"{r[0]}_utility",
                                                     "params": {"bassMonoHz": 150}}})
            if kick_id and (_four_on_floor(g) or st in ("offbeat", "rolling", "reese")):
                ops.append({"op": "plugin.add", "args": {"track": r[0], "plugin": "builtin:compressor", "id": f"{r[0]}_duck",
                                                         "sidechain": kick_id, "params": {"thresholdDb": -20, "ratio": 4,
                                                                                          "attack": 0.002, "release": 0.11}}})
    for role in ("chords", "pad"):
        if role not in want:
            continue
        if role == "chords":
            st = style_for["chords"]
            notes = gen_chords(cps, st, bar_qn, rng("chords"), center=60, voices=4)
        else:
            st = "pad"
            notes = gen_chords(cps, "pad", bar_qn, rng("pad"), center=57, voices=3)
        r = add_part(role, notes, st, target, p.get("preset"))
        if r and r[1]:
            ops.append({"op": "plugin.add", "args": {"track": r[0], "plugin": "builtin:eq", "id": f"{r[0]}_eq",
                                                     "params": {"lowCutHz": 180 if role == "chords" else 150}}})
            if role == "chords" and _four_on_floor(g) and st in ("supersaw", "pad"):
                ops.append({"op": "plugin.add", "args": {"track": r[0], "plugin": "builtin:volumeshaper", "id": f"{r[0]}_pump",
                                                         "params": {"rateBeats": 1, "depthDb": 12, "hold": 0.04, "recover": 0.5}}})
            rid = _return_for(doc, ops, ids, created_returns)
            ops.append({"op": "send.add", "args": {"track": r[0], "to": rid, "levelDb": -14 if role == "chords" else -10}})
    for role in ("lead", "arp"):
        if role not in want:
            continue
        if role == "lead":
            st = style_for["lead"]
            notes = gen_lead(cps, st, bar_qn, rng("lead"), tonic, scale, center=int(p.get("center", 72)))
        else:
            st = "arp"
            notes = gen_arp(cps, bar_qn, rng("arp"), center=int(p.get("center", 64)), rate=float(p.get("rate", 0.25)))
        r = add_part(role, notes, st, target, p.get("preset"))
        if r and r[1]:
            ops.append({"op": "plugin.add", "args": {"track": r[0], "plugin": "builtin:eq", "id": f"{r[0]}_eq",
                                                     "params": {"lowCutHz": 250, "peak2Hz": 3000, "peak2Db": 1.5}}})
            rid = _return_for(doc, ops, ids, created_returns)
            ops.append({"op": "send.add", "args": {"track": r[0], "to": rid, "levelDb": -15}})
    if part == "song":
        master = next(t for t in doc["tracks"] if t["type"] == "master")
        if not master.get("inserts"):
            ops += [{"op": "plugin.add", "args": {"track": master["id"], "plugin": "builtin:eq", "id": "master_eq",
                                                  "params": {"lowCutHz": 28, "highShelfHz": 10000, "highShelfDb": 1}}},
                    {"op": "plugin.add", "args": {"track": master["id"], "plugin": "builtin:compressor", "id": "master_comp",
                                                  "params": {"thresholdDb": -14, "ratio": 2, "attack": 0.02, "release": 0.2, "kneeDb": 8}}},
                    {"op": "plugin.add", "args": {"track": master["id"], "plugin": "builtin:limiter", "id": "master_limiter",
                                                  "params": {"ceilingDb": -1, "gainDb": 6}}}]
    styles = {"drums": g["name"], "bass": style_for["bass"], "chords": style_for["chords"], "pad": "pad",
              "lead": style_for["lead"], "arp": "arp"}
    what = ", ".join(f"{w} ({styles[w]})" for w in want)
    reason = (f"compose {part}: {what} in {info['key']} over {' '.join(info['chords'])}, {bars} bars at {at}, "
              f"{g['name']} style, seed {seed}" + (f", swing {swing:g}" if swing else ""))
    return {"ops": ops, "reason": p.get("reason") or reason, "info": info, "label": f"compose.{part}"}


def _run(s, plan, p, author):
    if p.get("dryRun"):
        return {"ok": True, "dryRun": True, "ops": plan["ops"], "reason": plan["reason"], **plan["info"]}
    res = s.apply("batch", {"ops": plan["ops"]}, author=author, reason=plan["reason"], label=plan["label"])
    res.pop("results", None)
    return {**res, "reason": plan["reason"], **plan["info"]}


def compose(s, p, author=None):
    plan = plan_compose(s, p)
    out = _run(s, plan, p, author)
    if plan["info"]["part"] == "song" and not p.get("dryRun") and str(p.get("arrange", True)).lower() not in ("false", "0", "no"):
        form = p.get("form") or th.genre(plan["info"]["genre"])["form"]
        out["arrange"] = arrange(s, {"form": form, "fx": p.get("fx", True)}, author)
    return out


# ------------------------------------------------------------------ arrange
# level offset (dB) per section kind, written as hold automation on a utility insert so the
# faders stay free: quieter intro/break/outro leave room for the drop to land
SECTION_DYNAMICS = {"intro": -4.0, "break": -4.0, "outro": -6.0, "verse": -2.0, "tension": -2.0}


def plan_arrange(s, p, doc=None):
    doc = copy.deepcopy(doc if doc is not None else s.doc)
    form_text = p.get("form") or th.genre(p.get("style") or p.get("genre"))["form"]
    try:
        form = th.parse_form(str(form_text))
    except th.TheoryError as e:
        raise MusicalError(str(e))
    tm, bar_qn, _ = _ctx(doc)
    start_bar = int(p.get("startBar", 1))
    ops, ids = [], _ids(doc)
    loops = {}  # track id -> (role, pattern, transpose)
    for t in doc["tracks"]:
        role = track_role(t)
        if not role or t["type"] != "instrument" or role in ("roll", "riser", "crash"):
            continue
        clips = [c for c in t.get("clips", []) if "pattern" in c]
        if not clips:
            continue
        first = min(clips, key=lambda c: tm.qn(c["at"]))
        loops[t["id"]] = (role, first["pattern"], first.get("transpose", 0))
        for c in t.get("clips", []):
            ops.append({"op": "clip.remove", "args": {"track": t["id"], "clip": c["id"]}})
    if not loops:
        raise MusicalError("nothing to arrange: compose some parts first (instrument tracks with patterns)")
    for m in doc.get("markers", []):
        ops.append({"op": "marker.remove", "args": {"name": m["name"]}})
    seen, layout, bar = {}, [], start_bar
    for kind_raw, nb in form:
        kind = th.section_kind(kind_raw)
        seen[kind_raw] = seen.get(kind_raw, 0) + 1
        nm = kind_raw.title() + (f" {seen[kind_raw]}" if seen[kind_raw] > 1 else "")
        layout.append((nm, kind, bar, nb))
        ops.append({"op": "marker.add", "args": {"name": nm, "at": _bar_pos(bar)}})
        bar += nb
    end_bar = bar
    placed = {}
    for tid, (role, pid, tr) in loops.items():
        for nm, kind, b0, nb in layout:
            roles = th.SECTION_ROLES.get(kind, set())
            if not (role in roles or EXTRA_ROLES.get(role) in roles):
                continue
            cid = _unique(ids, f"{tid}_{nm.lower().replace(' ', '')}")
            ids.add(cid)
            args = {"track": tid, "pattern": pid, "at": _bar_pos(b0), "lengthBeats": nb * bar_qn, "id": cid}
            if tr:
                args["transpose"] = tr
            ops.append({"op": "clip.add_pattern", "args": args})
            placed.setdefault(tid, []).append(nm)
    if str(p.get("dynamics", True)).lower() not in ("false", "0", "no"):
        pts = [[_bar_pos(b0), SECTION_DYNAMICS.get(kind, 0.0)] for nm, kind, b0, nb in layout]
        if any(v for _, v in pts):
            for tid in placed:
                t = _find_track(doc, tid)
                uid = f"{tid}_sections"
                if not any(i.get("id") == uid for i in t.get("inserts", [])):
                    ops.append({"op": "plugin.add", "args": {"track": tid, "plugin": "builtin:utility", "id": uid}})
                ops.append({"op": "auto.set", "args": {"track": tid, "target": f"inserts/{uid}/gainDb",
                                                       "points": pts, "curve": "hold"}})
    if str(p.get("fx", True)).lower() not in ("false", "0", "no"):
        builds = [(b0, nb) for nm, kind, b0, nb in layout if kind in ("build", "tension")]
        drops = [b0 for nm, kind, b0, nb in layout if kind in ("drop", "hook", "climax")]
        rid = next((t["id"] for t in doc["tracks"] if t["type"] == "return" and "verb" in t["id"].lower()), None)

        def fx_track(role, plugin, params):
            t = next((x for x in doc["tracks"] if track_role(x) == role), None)
            if t:
                for c in t.get("clips", []):
                    ops.append({"op": "clip.remove", "args": {"track": t["id"], "clip": c["id"]}})
                return t["id"], False
            tid = _unique(ids, ROLE_NAMES[role].lower().replace(" ", "_"))
            ids.add(tid)
            ops.append({"op": "track.add", "args": {"name": ROLE_NAMES[role], "id": tid, "plugin": plugin,
                                                    "params": params, "role": role}})
            ops.append({"op": "mix.set", "args": {"track": tid, "gainDb": ROLE_GAIN[role]}})
            if rid:
                ops.append({"op": "send.add", "args": {"track": tid, "to": rid, "levelDb": -8}})
            return tid, True

        if builds:
            roll, _ = fx_track("roll", "builtin:drums", _kv(DRUM_PRESETS["roll"]))
            riser, new_riser = fx_track("riser", "builtin:synth", synth_preset("riser", "*"))
            if new_riser or not any(i["id"] == f"{riser}_filter" for i in (_find_track(doc, riser) or {}).get("inserts", [])):
                ops.append({"op": "plugin.add", "args": {"track": riser, "plugin": "builtin:filter", "id": f"{riser}_filter",
                                                         "params": {"type": "bandpass", "cutoff": 400, "resonance": 0.4}}})
            shapes, made_rolls = [], {}
            for b0, nb in builds:
                rb = min(nb, 8)
                q = rb * bar_qn
                if rb not in made_rolls:
                    notes = []
                    for t0, t1, step in ((0, q / 2, 1.0), (q / 2, q * 0.75, 0.5), (q * 0.75, q - 1, 0.25), (q - 1, q, 0.125)):
                        t = t0
                        while t < t1 - 1e-9:
                            notes.append(_note(t, min(step, 0.25) * 0.8, 38, 40 + 87 * (t / q)))
                            t += step
                    pr = _unique(ids, f"roll_{rb}bars")
                    ids.add(pr)
                    ops.append({"op": "pattern.add", "args": {"id": pr, "name": f"Snare roll {rb} bars", "lengthBeats": q, "notes": notes}})
                    pz = _unique(ids, f"riser_{rb}bars")
                    ids.add(pz)
                    ops.append({"op": "pattern.add", "args": {"id": pz, "name": f"Riser {rb} bars", "lengthBeats": q,
                                                              "notes": [_note(0, q, 60, 100)]}})
                    made_rolls[rb] = (pr, pz)
                pr, pz = made_rolls[rb]
                ops.append({"op": "clip.add_pattern", "args": {"track": roll, "pattern": pr, "at": _bar_pos(b0 + nb - rb)}})
                ops.append({"op": "clip.add_pattern", "args": {"track": riser, "pattern": pz, "at": _bar_pos(b0 + nb - rb)}})
                shapes.append((_bar_pos(b0 + nb - rb), _bar_pos(b0 + nb)))
            # filter sweep: auto.set resets the lane (300 Hz), then one exp shape per build
            ops.append({"op": "auto.set", "args": {"track": riser, "target": f"inserts/{riser}_filter/cutoff",
                                                   "points": [["1.1.1", 300]]}})
            for a0, a1 in shapes:
                ops.append({"op": "auto.shape", "args": {"track": riser, "target": f"inserts/{riser}_filter/cutoff",
                                                         "start": a0, "end": a1, "from": 300, "to": 9000, "shape": "exp", "steps": 32}})
        if drops:
            crash, _ = fx_track("crash", "builtin:drums", _kv(DRUM_PRESETS["crash"]))
            pc = _unique(ids, "crash_hit")
            ids.add(pc)
            ops.append({"op": "pattern.add", "args": {"id": pc, "name": "Crash", "lengthBeats": bar_qn, "notes": [_note(0, 1, 49, 110)]}})
            for b0 in drops:
                ops.append({"op": "clip.add_pattern", "args": {"track": crash, "pattern": pc, "at": _bar_pos(b0)}})
    main = next(((b0, nb) for nm, kind, b0, nb in layout if kind in ("drop", "hook", "climax", "groove")), None)
    if main:
        ops.append({"op": "loop.set", "args": {"start": _bar_pos(main[0]), "end": _bar_pos(main[0] + min(8, main[1]))}})
    desc = ", ".join(f"{nm} {nb}" for nm, kind, b0, nb in layout)
    reason = p.get("reason") or (f"arrange: {desc} ({end_bar - start_bar} bars); parts placed: "
                                 + "; ".join(f"{tid} in {', '.join(v)}" for tid, v in placed.items()))
    return {"ops": ops, "reason": reason, "label": "arrange",
            "info": {"sections": [{"name": nm, "kind": kind, "bar": b0, "bars": nb} for nm, kind, b0, nb in layout],
                     "bars": end_bar - start_bar, "placed": placed}}


def arrange(s, p, author=None):
    return _run(s, plan_arrange(s, p), p, author)


# ------------------------------------------------------------------ clash: measure + fix
CLASH_BANDS = [(20, 60), (60, 120), (120, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000), (8000, 16000)]
PRIORITY = ["kick", "bass", "lead", "vocal", "clap", "snare", "chords", "arp", "pad", "hat", "fx", "riser"]


def clash_metrics(a: np.ndarray, b: np.ndarray, sr: int) -> dict:
    """How much track `b` covers track `a` (the one to protect), band by band.
    For each band and frame where `a` is active, the share of the band's power that belongs to
    `b`: PB / (PA + PB), weighted by `a`'s power. 0 = `a` is alone in its band, 50 = equal
    fight, 100 = buried. `index` averages the bands weighted by `a`'s energy in them."""
    from scipy import signal
    am = np.mean(a, axis=0) if a.ndim == 2 else a
    bm = np.mean(b, axis=0) if b.ndim == 2 else b
    n = 4096
    _, _, A = signal.stft(am, sr, nperseg=n, noverlap=n - 1024, boundary=None)
    _, _, B = signal.stft(bm, sr, nperseg=n, noverlap=n - 1024, boundary=None)
    f = np.fft.rfftfreq(n, 1 / sr)
    PA, PB = np.abs(A) ** 2, np.abs(B) ** 2
    bands, weights, shares = {}, [], []
    for lo, hi in CLASH_BANDS:
        sel = (f >= lo) & (f < hi)
        if not sel.any():
            continue
        pa, pb = PA[sel].sum(axis=0), PB[sel].sum(axis=0)
        if pa.max() <= 1e-12:
            bands[f"{lo}-{hi}"] = 0.0
            continue
        act = pa > pa.max() * 1e-3            # within 30 dB of a's own peak in this band
        w = pa * act
        share = float(np.sum(w * pb / (pa + pb + 1e-20)) / (np.sum(w) + 1e-20))
        bands[f"{lo}-{hi}"] = round(100 * share, 1)
        weights.append(float(pa.sum()))
        shares.append(share)
    wv, sv = np.array(weights), np.array(shares)
    idx = float(100 * np.sum(wv * sv) / max(float(np.sum(wv)), 1e-20)) if wv.size else 0.0
    # worst band among those where a has real energy (>= 5% of its total)
    energy = {}
    for (lo, hi) in CLASH_BANDS:
        sel = (f >= lo) & (f < hi)
        energy[f"{lo}-{hi}"] = float(PA[sel].sum()) / (float(PA.sum()) + 1e-20)
    cand = {k: v for k, v in bands.items() if energy.get(k, 0) >= 0.05} or bands
    worst = max(cand.items(), key=lambda kv: kv[1]) if cand else (None, 0.0)
    return {"index": round(idx, 1), "bands": bands, "worstBand": worst[0], "worstPct": worst[1]}


def _render_doc(s, doc, start=None, end=None, sr=None):
    from .render import render
    return render(doc, s.dir, sr=sr, start=start, end=end, stems=True)


def _transient_db(x, sr):
    from . import listen as L
    return float(L.transients(L._mono22(x, sr)).get("strengthDb") or 0.0)


def _prio(t):
    r = track_role(t) or "zz"
    return PRIORITY.index(r) if r in PRIORITY else len(PRIORITY)


def plan_fix_clash(s, p, doc=None):
    doc = copy.deepcopy(doc if doc is not None else s.doc)
    ta, tb = _find_track(doc, p.get("a")), _find_track(doc, p.get("b"))
    if ta is None or tb is None:
        raise MusicalError("fix clash needs two tracks, e.g. `fix clash kick bass`")
    if ta["id"] == tb["id"]:
        raise MusicalError("pick two different tracks")
    start, end = _default_range(doc, p)
    res = _render_doc(s, doc, start, end)
    if ta["id"] not in res.stems or tb["id"] not in res.stems:
        raise MusicalError("both tracks need audible output in the range")
    mode = str(p.get("mode", "auto")).lower()
    if mode not in ("auto", "sidechain", "eq"):
        raise MusicalError("mode must be auto, sidechain or eq")
    tra, trb = _transient_db(res.stems[ta["id"]], res.sr), _transient_db(res.stems[tb["id"]], res.sr)
    drums = ("kick", "clap", "snare", "hat", "roll", "crash")
    ra, rb = track_role(ta), track_role(tb)
    one_drum = (ra in drums) != (rb in drums)
    if one_drum:
        src, dst = (ta, tb) if ra in drums else (tb, ta)   # drums drive the duck
    elif ra and rb:
        src, dst = (ta, tb) if _prio(ta) <= _prio(tb) else (tb, ta)   # the more important part wins
    else:
        src, dst = (ta, tb) if tra >= trb else (tb, ta)    # else the punchier track does
    if any(i.get("sidechain") == dst["id"] for i in src.get("inserts", [])):
        src, dst = dst, src                                 # never build a sidechain loop
    before = clash_metrics(res.stems[src["id"]], res.stems[dst["id"]], res.sr)   # how much dst covers src
    ops, actions = [], []
    worst = before["worstBand"] or "60-120"
    lo, hi = (int(x) for x in worst.split("-"))
    centre = math.sqrt(lo * hi)
    depth = float(p.get("amount", 1.0))
    ids = _ids(doc)
    melodic_pair = bool(ra and rb and not one_drum and ra not in drums)
    if mode == "sidechain" or (mode == "auto" and not melodic_pair and (abs(tra - trb) > 3 or lo < 150)):
        existing = next((i for i in dst.get("inserts", []) if i["plugin"] == "builtin:compressor" and i.get("sidechain") == src["id"]), None)
        thr = round(-18 - 6 * depth, 1)
        if existing:
            cur = float((existing.get("params") or {}).get("thresholdDb", -18))
            thr = round(min(cur - 6 * depth, thr), 1)
            ops.append({"op": "plugin.set_params", "args": {"track": dst["id"], "plugin": existing["id"],
                                                             "params": {"thresholdDb": thr, "ratio": 6, "release": 0.12}}})
            actions.append(f"deepened the sidechain duck on {dst['name']} from {src['name']} (threshold {cur:g} -> {thr} dB, ratio 6)")
        else:
            pid = _unique(ids, f"{dst['id']}_duck")
            ops.append({"op": "plugin.add", "args": {"track": dst["id"], "plugin": "builtin:compressor", "id": pid,
                                                     "sidechain": src["id"], "params": {"thresholdDb": thr, "ratio": 5,
                                                                                        "attack": 0.002, "release": 0.1}}})
            actions.append(f"sidechain-ducked {dst['name']} from {src['name']} (threshold {thr} dB, ratio 5)")
    if mode == "eq" or (mode == "auto" and (lo >= 150 or not ops)):
        cut = dst
        cut_db = round(-min(6.0, 2.0 + 4.0 * before["worstPct"] / 100) * depth, 1)
        eqi = next((i for i in cut.get("inserts", []) if i["plugin"] == "builtin:eq"), None)
        if centre < 150:
            eq_params, how = {"lowCutHz": int(hi)}, f"high-passed {cut['name']} at {int(hi)} Hz"
        else:
            used = set()
            if eqi:
                used = {k[:5] for k, v in (eqi.get("params") or {}).items() if k.startswith("peak") and k.endswith("Db") and v}
            free = [sl for sl in ("peak1", "peak2", "peak3") if sl not in used] or ["peak3"]
            bands = sorted(((int(k.split("-")[0]), int(k.split("-")[1]), v) for k, v in before["bands"].items()),
                           key=lambda x: -x[2])
            picks = [b for b in bands if b[0] >= 150 and (b[2] >= 25 or b[0] == lo)][:min(2, len(free))] or [(lo, hi, before["worstPct"])]
            eq_params, parts = {}, []
            for (blo, bhi, pct), slot in zip(picks, free):
                c = int(round(math.sqrt(blo * bhi)))
                db = round(-min(6.0, 2.0 + 4.0 * pct / 100) * depth, 1)
                eq_params.update({f"{slot}Hz": c, f"{slot}Db": db, f"{slot}Q": 1.2})
                parts.append(f"{abs(db)} dB around {c} Hz")
            how = f"cut {cut['name']} by " + " and ".join(parts)
        if eqi:
            ops.append({"op": "plugin.set_params", "args": {"track": cut["id"], "plugin": eqi["id"], "params": eq_params}})
        else:
            ops.append({"op": "plugin.add", "args": {"track": cut["id"], "plugin": "builtin:eq",
                                                     "id": _unique(ids, f"{cut['id']}_fixeq"), "params": eq_params}})
        actions.append(how)
    new = doc
    for o in ops:
        new, _ = apply_op(new, o["op"], o["args"], Ctx(s.dir, "plan"))
    res2 = _render_doc(s, new, start, end)
    after = clash_metrics(res2.stems[src["id"]], res2.stems[dst["id"]], res2.sr)
    reason = p.get("reason") or (f"fix clash {src['name']} vs {dst['name']}: " + "; ".join(actions)
                                 + f". How much {dst['name']} covers {src['name']} (clash index): {before['index']} -> "
                                 f"{after['index']}; in {worst} Hz {before['worstPct']}% -> {after['bands'].get(worst)}%")
    return {"ops": ops, "reason": reason, "label": "fix.clash",
            "info": {"range": [start, end], "before": before, "after": after, "actions": actions, "source": src["id"], "target": dst["id"],
                     "improved": after["index"] < before["index"] * 0.9 and before["index"] - after["index"] >= 0.1}}


def fix_clash(s, p, author=None):
    plan = plan_fix_clash(s, p)
    info = plan["info"]
    if p.get("dryRun") or (not info["improved"] and not p.get("force")):
        out = {"ok": True, "applied": False, "reason": plan["reason"], **info}
        if not info["improved"]:
            out["note"] = "the fix did not reduce the clash, so nothing was changed (add --force true to apply anyway)"
        return out
    res = s.apply("batch", {"ops": plan["ops"]}, author=author, reason=plan["reason"], label="fix.clash")
    res.pop("results", None)
    return {**res, "applied": True, "reason": plan["reason"], **info}


def _default_range(doc, p):
    """Explicit start/end, a bar range, else the loop (usually the drop), else the whole song."""
    if p.get("bars"):
        return bar_range(doc, p["bars"])
    if p.get("start") is not None or p.get("end") is not None or p.get("whole"):
        return p.get("start"), p.get("end")
    lp = doc["transport"].get("loop") or {}
    tm = TimeMap(doc["transport"])
    if lp.get("start") and lp.get("end") and tm.qn(lp["end"]) > tm.qn(lp["start"]):
        return lp["start"], lp["end"]
    return None, None


def clash_report(s, p, author=None):
    """Read-only: clash index for every pair of the loudest instrument/audio stems."""
    start, end = _default_range(s.doc, p)
    res = s.render(start, end, stems=True)
    kinds = {t["id"]: t["type"] for t in s.doc["tracks"]}
    st = {k: v for k, v in res.stems.items() if kinds.get(k) in ("instrument", "audio")}
    lv = sorted(st, key=lambda k: -float(np.sqrt(np.mean(st[k] ** 2))))[: int(p.get("top", 6))]
    tr = {t["id"]: t for t in s.doc["tracks"]}
    pairs = []
    for i in range(len(lv)):
        for j in range(i + 1, len(lv)):
            a, b = (lv[i], lv[j]) if _prio(tr[lv[i]]) <= _prio(tr[lv[j]]) else (lv[j], lv[i])
            pairs.append({"a": a, "b": b, **clash_metrics(st[a], st[b], res.sr)})
    pairs.sort(key=lambda x: -x["index"])
    return {"range": [start, end], "pairs": pairs, "hint": f"try `fix clash {pairs[0]['a']} {pairs[0]['b']}`" if pairs else "no stems"}


# ------------------------------------------------------------------ variations
METRICS = ("loudness", "punch", "brightness", "darkness", "width", "clarity", "keyfit", "energy", "contrast", "tightness")


def score(metric: str, res, doc) -> float:
    """Higher is better for every metric (darkness = -centroid, tightness = -timing error)."""
    from .analysis import analyze as measure
    from . import listen as L
    x, sr = res.mix, res.sr
    if metric == "loudness":
        v = measure(x, sr)["master"]["integratedLufs"]
        return float(v if v is not None else -70.0)
    if metric == "width":
        return float(measure(x, sr)["master"]["stereoWidth"] or 0.0)
    m = L._mono22(x, sr)
    if metric == "punch":
        return float(L.transients(m).get("strengthDb") or 0.0)
    if metric in ("brightness", "darkness"):
        c = float(L.timbre(x, sr).get("centroidHz") or 0.0)
        return c if metric == "brightness" else -c
    if metric == "energy":
        return float(10 * np.log10(np.mean(m ** 2) + 1e-12))
    if metric == "contrast":
        tm, bar_qn, bpm = _ctx(doc)
        c = np.array(L.energy_curve(x, sr, bar_qn * 60 / bpm))
        return float(c.max() - np.percentile(c, 10)) if c.size else 0.0
    if metric == "keyfit":
        prof = L.chroma(L._stft(m)).sum(axis=1)
        text = doc["transport"].get("keySig")
        if not text:
            return float(L.estimate_key(L.chroma(L._stft(m))).get("confidence") or 0.0)
        tonic, scale = th.parse_key(text)
        return float(prof[th.scale_pcs(tonic, scale)].sum() / (prof.sum() + 1e-12))
    if metric == "tightness":
        env = L.onset_envelope(m)
        t = L.timing(env, _ctx(doc)[2], 0.0, m=m)
        return -float(t.get("medianDevMs") if t.get("medianDevMs") is not None else 99.0)
    if metric == "clarity":
        st = {k: v for k, v in res.stems.items() if k != "master"}
        names = [t["id"] for t in doc["tracks"] if t["type"] in ("instrument", "audio") and t["id"] in st]
        lv = sorted(names, key=lambda k: -float(np.sqrt(np.mean(st[k] ** 2))))[:4]
        idx = [clash_metrics(st[a], st[b], sr)["index"] for i, a in enumerate(lv) for b in lv[i + 1:]]
        return float(100 - (np.mean(idx) if idx else 0.0))
    raise MusicalError(f"unknown metric {metric!r} (choose {', '.join(METRICS)})")


def _parse_command(cmd):
    if isinstance(cmd, dict):
        return cmd.get("method"), dict(cmd.get("params") or {})
    import shlex
    from .cli import build_call
    method, params, _ = build_call(shlex.split(str(cmd)))
    return method, params


def _plan_ops(s, method, params, doc):
    if method in MUSICAL_PLANNERS:
        plan = MUSICAL_PLANNERS[method](s, params, doc)
        return [{"op": "batch", "args": {"ops": plan["ops"]}}]
    from .ops import REGISTRY
    if method in REGISTRY:
        return [{"op": method, "args": params}]
    raise MusicalError(f"variations can try compose, arrange, humanize or any op, not {method!r}")


def variations(s, p, author=None):
    n = int(p.get("n") or p.get("count") or 4)
    if not 1 <= n <= 32:
        raise MusicalError("n must be 1..32")
    metric, _, want = str(p.get("target") or "clarity").partition("=")
    metric = metric.strip().lower()
    if metric not in METRICS:
        raise MusicalError(f"unknown metric {metric!r} (choose {', '.join(METRICS)}; add =value to aim at a number)")
    cmd = p.get("command") or p.get("cmd") or "compose lead"
    if isinstance(cmd, (list, tuple)):
        import shlex
        cmd = " ".join(shlex.quote(str(w)) for w in cmd)
    method, base = _parse_command(cmd)
    cands = []
    if p.get("vary"):
        name, _, span = str(p["vary"]).partition("=")
        lo, _, hi = span.partition("..")
        try:
            lo, hi = float(lo), float(hi)
        except ValueError:
            raise MusicalError("vary must look like name=lo..hi, e.g. gainDb=-12..-3 or params.cutoff=300..3000")
        for i in range(n):
            v = round(lo + (hi - lo) * (i / max(1, n - 1)), 4)
            pp = copy.deepcopy(base)
            if "." in name:
                a, b = name.split(".", 1)
                pp[a] = dict(pp.get(a) or {})
                pp[a][b] = v
            else:
                pp[name] = v
            cands.append((f"{name}={v:g}", pp))
    else:
        s0 = int(base.get("seed", p.get("seed", 1)))
        cands = [(f"seed {s0 + i}", {**base, "seed": s0 + i}) for i in range(n)]
    tm, bar_qn, _ = _ctx(s.doc)
    start, end = p.get("start"), p.get("end")
    if start is None and method == "compose" and base.get("at") is None and s.doc.get("markers"):
        start, end = _default_range(s.doc, {})          # arranged song: judge it in the loop (first drop)
    if start is None and method == "compose":
        at = base.get("at") or "1.1.1"
        bars = min(int(base.get("bars") or 8), int(p.get("previewBars", 8)))
        start, end = at, _qn_to_pos(tm, bar_qn, tm.qn(at) + bars * bar_qn)
    sr = int(p.get("sr", 24000))
    rows = []
    for label, pp in cands:
        try:
            d = copy.deepcopy(s.doc)
            for o in _plan_ops(s, method, pp, d):
                d, _ = apply_op(d, o["op"], o["args"], Ctx(s.dir, "plan"))
            v = score(metric, _render_doc(s, d, start, end, sr=sr), d)
            rows.append({"label": label, "params": pp, "value": round(v, 3),
                         "score": round(-abs(v - float(want)) if want else v, 3)})
        except (OpError, ValueError) as e:
            rows.append({"label": label, "params": pp, "error": str(e)})
    ok = [r for r in rows if "score" in r]
    if not ok:
        raise MusicalError("every variation failed: " + "; ".join(r.get("error", "") for r in rows))
    best = max(ok, key=lambda r: r["score"])  # first wins ties: deterministic
    out = {"metric": metric, "target": want or None, "range": [start, end], "command": method,
           "results": [{k: v for k, v in r.items() if k != "params"} for r in rows], "best": best["label"]}
    if str(p.get("keep", "best")).lower() == "list" or p.get("dryRun"):
        out["applied"] = False
        return out
    others = ", ".join(f"{r['label']}={r['value']}" for r in ok if r is not best)
    reason = (f"variations: tried {len(rows)} x {method}, kept {best['label']} for the best {metric}"
              f"{' near ' + want if want else ''} ({best['value']}; others {others or 'none'})")
    pp = dict(best["params"])
    if method in MUSICAL_RUNNERS:
        r = MUSICAL_RUNNERS[method](s, {**pp, "reason": reason}, author)
    else:
        r = s.apply(method, pp, author=author, reason=reason, label=f"variations:{method}")
    out.update({"applied": True, "n": r.get("n"), "reason": reason})
    return out


# ------------------------------------------------------------------ humanize
def _spread_ms(notes, bpm, grid=0.25):
    if not notes:
        return 0.0
    d = [abs(n["t"] - round(n["t"] / grid) * grid) for n in notes]
    return round(float(np.median(d)) * 60000 / bpm, 2)


HUMAN_FEELS = {"tight": (4, 4, 0.0), "natural": (9, 8, 0.0), "loose": (18, 14, 0.0), "swing": (6, 8, 0.12),
               "drunk": (28, 18, 0.18)}


def plan_humanize(s, p, doc=None):
    doc = copy.deepcopy(doc if doc is not None else s.doc)
    tm, bar_qn, bpm = _ctx(doc)
    if p.get("pattern"):
        if p["pattern"] not in doc["patterns"]:
            raise MusicalError(f"no pattern {p['pattern']!r}")
        pats = [p["pattern"]]
    else:
        t = _find_track(doc, p.get("track"))
        if t is None:
            raise MusicalError("humanize needs a track name or --pattern")
        pats = sorted({c["pattern"] for c in t.get("clips", []) if "pattern" in c})
        if not pats:
            raise MusicalError(f"{t['name']} has no MIDI patterns")
    feel = str(p.get("feel", "natural")).lower()
    if feel not in HUMAN_FEELS:
        raise MusicalError(f"feel must be one of {', '.join(HUMAN_FEELS)}")
    ms, vel, sw = HUMAN_FEELS[feel]
    amt = float(p.get("amount", 1.0))
    ms, vel = float(p.get("timingMs", ms)) * amt, float(p.get("velocity", vel)) * amt
    sw = float(p.get("swing", sw))
    seed = int(p.get("seed", 1))
    qn = ms / 1000.0 * bpm / 60.0
    ops, before = [], {}
    for i, pid in enumerate(pats):
        before[pid] = _spread_ms(doc["patterns"][pid]["notes"], bpm)
        if sw > 0:
            ops.append({"op": "pattern.swing", "args": {"pattern": pid, "amount": round(sw, 3), "grid": float(p.get("grid", 0.25))}})
        ops.append({"op": "pattern.humanize", "args": {"pattern": pid, "timing": round(min(qn, 0.25), 5),
                                                       "velocity": round(vel, 2), "seed": seed + i}})
    d = doc
    for o in ops:
        d, _ = apply_op(d, o["op"], o["args"], Ctx(s.dir, "plan"))
    after = {pid: _spread_ms(d["patterns"][pid]["notes"], bpm) for pid in pats}
    reason = p.get("reason") or (f"humanize ({feel}): timing +-{ms:.0f} ms, velocity +-{vel:.0f}"
                                 + (f", swing {sw:g}" if sw else "") + f", seed {seed}; median distance from the 1/16 grid "
                                 + ", ".join(f"{k} {before[k]} -> {after[k]} ms" for k in pats))
    return {"ops": ops, "reason": reason, "label": "humanize", "info": {"patterns": pats, "beforeMs": before, "afterMs": after}}


def humanize(s, p, author=None):
    return _run(s, plan_humanize(s, p), p, author)


# ------------------------------------------------------------------ preview
def bar_range(doc, text):
    tm, bar_qn, _ = _ctx(doc)
    m = re.fullmatch(r"\s*(\d+)\s*(?:-|\.\.|to)\s*(\d+)\s*", str(text))
    if m:
        return _bar_pos(int(m.group(1))), _bar_pos(int(m.group(2)) + 1)
    m = re.fullmatch(r"\s*(\d+)\s*", str(text))
    if m:
        return _bar_pos(int(m.group(1))), _bar_pos(int(m.group(1)) + 1)
    secs = sorted(doc.get("markers", []), key=lambda x: tm.qn(x["at"]))
    for i, mk in enumerate(secs):
        if mk["name"].lower() == str(text).lower():
            return mk["at"], (secs[i + 1]["at"] if i + 1 < len(secs) else None)
    raise MusicalError(f"bad bar range {text!r} (use 9-12, a bar number, or a section name)")


def preview(s, p, author=None):
    import time
    import soundfile as sf
    doc = copy.deepcopy(s.doc)
    start, end = p.get("start"), p.get("end")
    if p.get("bars"):
        start, end = bar_range(doc, p["bars"])
    elif start is None and end is None:
        lp = doc["transport"].get("loop") or {}
        start, end = lp.get("start"), lp.get("end")
    tracks = p.get("tracks") or p.get("track")
    if isinstance(tracks, str):
        tracks = [x for x in re.split(r"[,\s]+", tracks) if x]
    if tracks:
        keep = []
        for r in tracks:
            t = _find_track(doc, r)
            if t is None:
                raise MusicalError(f"no track {r!r}")
            keep.append(t["id"])
        for t in doc["tracks"]:
            if t["type"] in ("instrument", "audio", "bus"):
                t.setdefault("mix", {})["solo"] = t["id"] in keep
    sr = int(p.get("sr") or doc["settings"].get("sampleRate", 48000))
    t0 = time.time()
    res = _render_doc(s, doc, start, end, sr=sr)
    took = time.time() - t0
    out = Path(p.get("out") or "renders/preview.wav")
    out = out if out.is_absolute() else s.dir / out
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), res.mix.T.astype(np.float32), res.sr, subtype="PCM_24")
    rep = {"ok": True, "path": str(out), "start": start, "end": end, "tracks": tracks or "all",
           "durationSec": round(res.duration, 2), "renderSec": round(took, 2), "sampleRate": res.sr}
    if str(p.get("listen", True)).lower() not in ("false", "0", "no"):
        from .analysis import analyze as measure
        from .listen import listen as L
        tm, bar_qn, bpm = _ctx(doc)
        r = L(res.mix, res.sr, bpm_hint=bpm, bar_sec=bar_qn * 60 / bpm)
        rep["listen"] = {"summary": r.get("summary"), "key": r["key"].get("key"), "bpm": r["tempo"].get("bpm"),
                         "tags": r["timbre"]["mix"].get("tags"),
                         "lufs": measure(res.mix, res.sr)["master"]["integratedLufs"]}
    if getattr(res, "warnings", None):
        rep["warnings"] = res.warnings
    return rep


# ------------------------------------------------------------------ instrument add (kit names)
def instrument_add(s, p, author=None):
    name = str(p.get("plugin") or "").strip()
    if not name:
        raise MusicalError("instrument add <surge|sfizz|synth|drums|sampler|clap:ID|vst3:NAME> [--name N] [--preset P] [--sfz piano]")
    low = name.lower()
    tname = p.get("name") or {"surge": "Surge", "sfizz": "Sfizz"}.get(low, name.split(":")[-1].replace(".", " ").title())
    tid = _unique(_ids(s.doc), str(tname).lower().replace(" ", "_"))
    args = {"name": tname, "id": tid}
    label = name
    if low in ("synth", "drums", "sampler"):
        args["plugin"] = f"builtin:{low}"
    elif ":" in name:
        args["plugin"] = name
    else:
        from . import kit
        spec = kit.instrument_spec(low, preset=p.get("preset"), sfz=p.get("sfz"), project_dir=s.dir)
        args.update({k: v for k, v in spec.items() if k in ("plugin", "path", "params", "state")})
        label = spec.get("label", name)
    if p.get("role"):
        args["role"] = p["role"]
    reason = p.get("reason") or f"instrument add: {tname} using {label}"
    res = s.apply("track.add", args, author=author, reason=reason, label="instrument.add")
    return {**res, "track": tid, "instrument": label}


MUSICAL_PLANNERS = {"compose": plan_compose, "arrange": plan_arrange, "humanize": plan_humanize}
MUSICAL_RUNNERS = {"compose": compose, "arrange": arrange, "humanize": humanize, "fix.clash": fix_clash}

QUERIES = {
    "compose": compose, "arrange": arrange, "fix.clash": fix_clash, "clash.report": clash_report,
    "variations": variations, "humanize": humanize, "preview": preview, "instrument.add": instrument_add,
}
POSITIONAL = {"compose": ["part"], "arrange": ["form"], "fix.clash": ["a", "b"], "variations": ["n", "command*"],
              "humanize": ["track"], "preview": ["bars"], "instrument.add": ["plugin"], "listen": ["reference"]}
HELP = {
    "compose": "Write a part as one undo step: compose <bass|chords|pad|lead|arp|drums|song> {key, chords, bars, style|genre, seed, at, track, engine auto|builtin|surge, preset}",
    "arrange": "Lay the composed parts out into a song form with markers, rolls, risers and crashes: arrange 'intro:8,build:8,drop:16' {fx}",
    "fix.clash": "Measure two tracks fighting and fix it (sidechain / EQ) with before/after numbers: fix clash <a> <b> {mode auto|sidechain|eq, start, end, amount}",
    "clash.report": "Read-only clash index for each pair of the loudest stems {start, end, top}",
    "variations": "Try N versions of a command on scratch copies, score them, keep the best or list: variations <n> {command, target metric[=value], vary name=lo..hi, keep best|list}",
    "humanize": "Loosen timing and velocity, optional swing: humanize <track> {feel tight|natural|loose|swing|drunk, amount, swing, seed}",
    "preview": "Quick render of a bar range, section or soloed tracks to renders/preview.wav with a listen summary: preview <9-12|Drop> {tracks, sr}",
    "instrument.add": "Add an instrument track by name: instrument add <surge|sfizz|synth|drums|sampler> {name, preset, sfz, role}",
}
