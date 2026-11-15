"""The op layer: every change to a project is a named op with validated args.

An op handler receives (doc, args, ctx) and mutates `doc` (a private copy). The
session diffs before/after to build the undo patch, so handlers stay simple.
Handlers may return a dict that is merged into the op result (e.g. new ids).
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import dsp
from .model import (ModelError, TRACK_TYPES, all_ids, clip as find_clip, insert as find_insert,
                    pattern as find_pattern, track as find_track, unique_id)
from .timebase import TimeMap, note_number


class OpError(ValueError):
    pass


@dataclass
class OpSpec:
    name: str
    fn: Callable
    summary: str
    args: dict            # name -> (type, required, description)
    destructive: bool = False
    positional: list = field(default_factory=list)


REGISTRY: dict[str, OpSpec] = {}


def op(name: str, summary: str, args: dict | None = None, destructive: bool = False, positional=None):
    def deco(fn):
        REGISTRY[name] = OpSpec(name, fn, summary, args or {}, destructive, positional or [])
        return fn
    return deco


@dataclass
class Ctx:
    project_dir: Path
    author: str = "human"


def _req(args, k):
    if k not in args or args[k] is None:
        raise OpError(f"missing argument '{k}'")
    return args[k]


def _num(v, name, lo=None, hi=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise OpError(f"'{name}' must be a number, got {v!r}")
    if math.isnan(f) or math.isinf(f):
        raise OpError(f"'{name}' must be finite")
    if lo is not None and f < lo or hi is not None and f > hi:
        raise OpError(f"'{name}'={f} out of range [{lo}, {hi}]")
    return f


def _pos(doc, v, name="at"):
    try:
        TimeMap(doc["transport"]).qn(v)
    except Exception as e:
        raise OpError(f"bad position for '{name}': {e}")
    return v


def validate_args(spec: OpSpec, args: dict) -> dict:
    out = dict(args)
    for k, (typ, required, _d) in spec.args.items():
        if required and (k not in out or out[k] is None):
            raise OpError(f"{spec.name}: missing argument '{k}'")
    unknown = [k for k in out if k not in spec.args and k not in ("dryRun", "confirm")]
    if unknown:
        raise OpError(f"{spec.name}: unknown argument(s) {', '.join(unknown)} (allowed: {', '.join(spec.args)})")
    return out


A = lambda typ, req=False, desc="": (typ, req, desc)


# ============================================================ project / time
@op("project.set", "Set title, author, sample rate, seed, tail or sandbox setting",
    {"title": A("string"), "author": A("string"), "sampleRate": A("integer"), "seed": A("integer"),
     "tailSec": A("number"), "pluginSandbox": A("boolean")})
def _project_set(doc, a, ctx):
    for k in ("title", "author"):
        if k in a:
            doc["meta"][k] = str(a[k])
    s = doc["settings"]
    if "sampleRate" in a:
        sr = int(a["sampleRate"])
        if sr not in (22050, 32000, 44100, 48000, 88200, 96000, 192000):
            raise OpError(f"unsupported sample rate {sr}")
        s["sampleRate"] = sr
    if "seed" in a:
        s["seed"] = int(a["seed"])
    if "tailSec" in a:
        s["tailSec"] = _num(a["tailSec"], "tailSec", 0, 60)
    if "pluginSandbox" in a:
        s["pluginSandbox"] = bool(a["pluginSandbox"])


@op("tempo.set", "Set the tempo (at the start, or add/replace a tempo change at a position)",
    {"bpm": A("number", True), "at": A("position")}, positional=["bpm"])
def _tempo_set(doc, a, ctx):
    bpm = _num(a["bpm"], "bpm", 20, 999)
    at = a.get("at", "1.1.1")
    tm = TimeMap(doc["transport"])
    q = tm.qn(_pos(doc, at))
    lst = doc["transport"].setdefault("tempo", [])
    for t in lst:
        if abs(tm.qn(t["at"]) - q) < 1e-9:
            t["bpm"] = bpm
            break
    else:
        lst.append({"at": at, "bpm": bpm})
        lst.sort(key=lambda t: tm.qn(t["at"]))


@op("tempo.remove", "Remove a tempo change", {"at": A("position", True)}, positional=["at"])
def _tempo_remove(doc, a, ctx):
    tm = TimeMap(doc["transport"])
    q = tm.qn(a["at"])
    lst = doc["transport"]["tempo"]
    keep = [t for t in lst if abs(tm.qn(t["at"]) - q) > 1e-9]
    if len(keep) == len(lst):
        raise OpError(f"no tempo change at {a['at']}")
    if not keep:
        raise OpError("cannot remove the only tempo")
    doc["transport"]["tempo"] = keep


@op("timesig.set", "Set a time signature (at a bar start)",
    {"num": A("integer", True), "den": A("integer", True), "at": A("position")}, positional=["num", "den"])
def _timesig_set(doc, a, ctx):
    num, den = int(a["num"]), int(a["den"])
    if not 1 <= num <= 32 or den not in (1, 2, 4, 8, 16, 32):
        raise OpError("bad time signature")
    at = a.get("at", "1.1.1")
    lst = doc["transport"].setdefault("timeSig", [])
    for t in lst:
        if str(t["at"]).split(".")[0] == str(at).split(".")[0]:
            t["num"], t["den"] = num, den
            break
    else:
        lst.append({"at": at, "num": num, "den": den})
    TimeMap(doc["transport"])  # validates


@op("keysig.set", "Set the song key (metadata, e.g. 'F minor')", {"key": A("string", True)}, positional=["key"])
def _keysig(doc, a, ctx):
    doc["transport"]["keySig"] = str(a["key"])


@op("loop.set", "Set the loop range", {"start": A("position", True), "end": A("position", True)},
    positional=["start", "end"])
def _loop(doc, a, ctx):
    tm = TimeMap(doc["transport"])
    if tm.qn(a["end"]) <= tm.qn(a["start"]):
        raise OpError("loop end must be after start")
    doc["transport"]["loop"] = {"start": a["start"], "end": a["end"]}


@op("marker.add", "Add a named marker / section start", {"name": A("string", True), "at": A("position", True)},
    positional=["name", "at"])
def _marker_add(doc, a, ctx):
    _pos(doc, a["at"])
    if any(m["name"] == a["name"] for m in doc["markers"]):
        raise OpError(f"marker {a['name']!r} exists")
    doc["markers"].append({"at": a["at"], "name": a["name"]})
    tm = TimeMap(doc["transport"])
    doc["markers"].sort(key=lambda m: tm.qn(m["at"]))


@op("marker.move", "Move a marker", {"name": A("string", True), "at": A("position", True)}, positional=["name", "at"])
def _marker_move(doc, a, ctx):
    _pos(doc, a["at"])
    for m in doc["markers"]:
        if m["name"] == a["name"]:
            m["at"] = a["at"]
            tm = TimeMap(doc["transport"])
            doc["markers"].sort(key=lambda m: tm.qn(m["at"]))
            return
    raise OpError(f"no marker {a['name']!r}")


@op("marker.remove", "Remove a marker", {"name": A("string", True)}, positional=["name"])
def _marker_remove(doc, a, ctx):
    n = len(doc["markers"])
    doc["markers"] = [m for m in doc["markers"] if m["name"] != a["name"]]
    if len(doc["markers"]) == n:
        raise OpError(f"no marker {a['name']!r}")


# ============================================================ tracks
@op("track.add", "Add a track (audio, instrument, bus, return)",
    {"name": A("string", True), "type": A("string"), "id": A("string"), "output": A("string"),
     "plugin": A("string", desc="instrument plugin, e.g. builtin:synth, clap:<id>, vst3:<name>"),
     "params": A("object"), "path": A("string"), "color": A("string"), "index": A("integer")},
    positional=["name"])
def _track_add(doc, a, ctx):
    typ = a.get("type", "instrument" if a.get("plugin") else "audio")
    if typ not in TRACK_TYPES or typ == "master":
        raise OpError(f"type must be one of audio, instrument, bus, return (got {typ!r})")
    ids = all_ids(doc)
    tid = a.get("id") or unique_id(ids, a["name"])
    if tid in ids:
        raise OpError(f"id {tid!r} already used")
    out = a.get("output", "master")
    find_track(doc, out)
    t = {"id": tid, "type": typ, "name": a["name"]}
    if a.get("color"):
        t["color"] = a["color"]
    if typ == "instrument":
        inst = {"plugin": a.get("plugin", "builtin:synth")}
        if a.get("params"):
            inst["params"] = dict(a["params"])
        if a.get("path"):
            inst["path"] = a["path"]
        _check_plugin_ref(inst["plugin"], instrument=True)
        t["instrument"] = inst
    if typ in ("audio", "instrument"):
        t["clips"] = []
    t.update({"inserts": [], "mix": {"gainDb": 0.0, "pan": 0.0}, "sends": [], "output": out})
    tracks = doc["tracks"]
    master_i = next(i for i, x in enumerate(tracks) if x["type"] == "master")
    idx = int(a["index"]) if "index" in a else master_i
    tracks.insert(max(0, min(idx, len(tracks))), t)
    return {"id": tid}


@op("track.remove", "Delete a track (and sends/routes that point to it)", {"track": A("string", True)},
    destructive=True, positional=["track"])
def _track_remove(doc, a, ctx):
    t = find_track(doc, a["track"])
    if t["type"] == "master":
        raise OpError("cannot remove master")
    doc["tracks"].remove(t)
    for o in doc["tracks"]:
        if o.get("output") == t["id"]:
            o["output"] = "master"
        o["sends"] = [s for s in o.get("sends", []) if s["to"] != t["id"]]
        for p in o.get("inserts", []):
            if p.get("sidechain") == t["id"]:
                p.pop("sidechain")


@op("track.rename", "Rename a track", {"track": A("string", True), "name": A("string", True)},
    positional=["track", "name"])
def _track_rename(doc, a, ctx):
    find_track(doc, a["track"])["name"] = str(a["name"])


@op("track.set", "Set track colour", {"track": A("string", True), "color": A("string")}, positional=["track"])
def _track_set(doc, a, ctx):
    t = find_track(doc, a["track"])
    if "color" in a:
        t["color"] = a["color"]


@op("track.route", "Route a track's output to a bus/return/master", {"track": A("string", True), "to": A("string", True)},
    positional=["track", "to"])
def _track_route(doc, a, ctx):
    t = find_track(doc, a["track"])
    dst = find_track(doc, a["to"])
    if dst["type"] not in ("bus", "return", "master") or dst is t:
        raise OpError("can only route to a bus, return or master")
    t["output"] = dst["id"]
    from .graph import order
    order(doc)


@op("track.move", "Reorder a track", {"track": A("string", True), "index": A("integer", True)},
    positional=["track", "index"])
def _track_move(doc, a, ctx):
    t = find_track(doc, a["track"])
    doc["tracks"].remove(t)
    doc["tracks"].insert(max(0, min(int(a["index"]), len(doc["tracks"]))), t)


@op("track.duplicate", "Duplicate a track with its clips and plugins", {"track": A("string", True), "name": A("string")},
    positional=["track"])
def _track_dup(doc, a, ctx):
    t = find_track(doc, a["track"])
    if t["type"] == "master":
        raise OpError("cannot duplicate master")
    n = copy.deepcopy(t)
    ids = all_ids(doc)
    n["name"] = a.get("name") or f"{t['name']} copy"
    n["id"] = unique_id(ids, n["name"]); ids.add(n["id"])
    for c in n.get("clips", []):
        c["id"] = unique_id(ids, f"{n['id']}_c"); ids.add(c["id"])
    for p in n.get("inserts", []):
        p["id"] = unique_id(ids, f"{n['id']}_fx"); ids.add(p["id"])
    doc["tracks"].insert(doc["tracks"].index(t) + 1, n)
    return {"id": n["id"]}


# ============================================================ mixer
@op("mix.set", "Set track fader gain (dB), pan (-1..1), mute or solo",
    {"track": A("string", True), "gainDb": A("number"), "pan": A("number"), "mute": A("boolean"), "solo": A("boolean")},
    positional=["track"])
def _mix_set(doc, a, ctx):
    t = find_track(doc, a["track"])
    m = t.setdefault("mix", {})
    if "gainDb" in a:
        m["gainDb"] = _num(a["gainDb"], "gainDb", -144, 24)
    if "pan" in a:
        m["pan"] = _num(a["pan"], "pan", -1, 1)
    for k in ("mute", "solo"):
        if k in a:
            if bool(a[k]):
                m[k] = True
            else:
                m.pop(k, None)
    if not any(k in a for k in ("gainDb", "pan", "mute", "solo")):
        raise OpError("give gainDb, pan, mute or solo")


@op("send.add", "Send a track to a return/bus", {"track": A("string", True), "to": A("string", True),
    "levelDb": A("number"), "pre": A("boolean")}, positional=["track", "to"])
def _send_add(doc, a, ctx):
    t = find_track(doc, a["track"]); dst = find_track(doc, a["to"])
    if dst["type"] not in ("return", "bus") or dst is t:
        raise OpError("sends go to a return or bus track")
    if any(s["to"] == dst["id"] for s in t.get("sends", [])):
        raise OpError(f"{t['id']} already sends to {dst['id']}")
    t.setdefault("sends", []).append({"to": dst["id"], "levelDb": _num(a.get("levelDb", -12), "levelDb", -144, 12),
                                       "pre": bool(a.get("pre", False))})
    from .graph import order
    order(doc)


@op("send.set", "Change a send level or pre/post", {"track": A("string", True), "to": A("string", True),
    "levelDb": A("number"), "pre": A("boolean")}, positional=["track", "to"])
def _send_set(doc, a, ctx):
    t = find_track(doc, a["track"]); dst = find_track(doc, a["to"])
    for s in t.get("sends", []):
        if s["to"] == dst["id"]:
            if "levelDb" in a:
                s["levelDb"] = _num(a["levelDb"], "levelDb", -144, 12)
            if "pre" in a:
                s["pre"] = bool(a["pre"])
            return
    raise OpError(f"{t['id']} has no send to {dst['id']}")


@op("send.remove", "Remove a send", {"track": A("string", True), "to": A("string", True)}, positional=["track", "to"])
def _send_remove(doc, a, ctx):
    t = find_track(doc, a["track"]); dst = find_track(doc, a["to"])
    n = len(t.get("sends", []))
    t["sends"] = [s for s in t.get("sends", []) if s["to"] != dst["id"]]
    if len(t["sends"]) == n:
        raise OpError(f"{t['id']} has no send to {dst['id']}")


# ============================================================ plugins
def _check_plugin_ref(ref: str, instrument: bool):
    fmt, _, name = str(ref).partition(":")
    if fmt == "builtin":
        ok = name in dsp.INSTRUMENTS if instrument else name in dsp.FX
        if not ok:
            choices = dsp.INSTRUMENTS if instrument else tuple(dsp.FX)
            raise OpError(f"unknown built-in {'instrument' if instrument else 'effect'} {name!r} "
                          f"(choose {', '.join(choices)})")
    elif fmt not in ("clap", "vst3"):
        raise OpError(f"plugin ref must be builtin:<name>, clap:<id> or vst3:<name>, got {ref!r}")


def _check_params(ref, params):
    fmt, _, name = str(ref).partition(":")
    if fmt == "builtin" and params:
        known = set(dsp.PARAMS[name]) | ({"zones"} if name == "sampler" else set())
        bad = [k for k in params if k not in known or k.startswith("_")]
        if bad:
            raise OpError(f"unknown {name} param(s): {', '.join(bad)} (known: "
                          f"{', '.join(k for k in dsp.PARAMS[name] if not k.startswith('_'))})")


@op("plugin.add", "Add an effect to a track's insert chain",
    {"track": A("string", True), "plugin": A("string", True), "params": A("object"), "slot": A("integer"),
     "id": A("string"), "path": A("string"), "sidechain": A("string")}, positional=["track", "plugin"])
def _plugin_add(doc, a, ctx):
    t = find_track(doc, a["track"])
    _check_plugin_ref(a["plugin"], instrument=False)
    _check_params(a["plugin"], a.get("params"))
    ids = all_ids(doc)
    pid = a.get("id") or unique_id(ids, f"{t['id']}_{a['plugin'].split(':', 1)[1].split('.')[-1]}")
    if pid in ids:
        raise OpError(f"id {pid!r} already used")
    p = {"id": pid, "plugin": a["plugin"]}
    if a.get("params"):
        p["params"] = dict(a["params"])
    if a.get("path"):
        p["path"] = a["path"]
    if a.get("sidechain"):
        p["sidechain"] = find_track(doc, a["sidechain"])["id"]
    lst = t.setdefault("inserts", [])
    slot = int(a.get("slot", len(lst)))
    lst.insert(max(0, min(slot, len(lst))), p)
    from .graph import order
    order(doc)
    return {"id": pid}


@op("plugin.remove", "Remove an insert", {"track": A("string", True), "plugin": A("string", True)},
    positional=["track", "plugin"])
def _plugin_remove(doc, a, ctx):
    t = find_track(doc, a["track"])
    t["inserts"].remove(find_insert(t, a["plugin"]))


@op("plugin.bypass", "Bypass or enable an insert", {"track": A("string", True), "plugin": A("string", True),
    "bypass": A("boolean")}, positional=["track", "plugin"])
def _plugin_bypass(doc, a, ctx):
    p = find_insert(find_track(doc, a["track"]), a["plugin"])
    if a.get("bypass", True):
        p["bypass"] = True
    else:
        p.pop("bypass", None)


@op("plugin.set_params", "Set insert or instrument parameters",
    {"track": A("string", True), "plugin": A("string", desc="insert id; omit or 'instrument' for the instrument"),
     "params": A("object", True)}, positional=["track", "plugin"])
def _plugin_set(doc, a, ctx):
    t = find_track(doc, a["track"])
    if not isinstance(a["params"], dict) or not a["params"]:
        raise OpError("params must be a non-empty object")
    if a.get("plugin") in (None, "instrument"):
        if "instrument" not in t:
            raise OpError(f"{t['id']} has no instrument")
        p = t["instrument"]
    else:
        p = find_insert(t, a["plugin"])
    _check_params(p["plugin"], a["params"])
    p.setdefault("params", {}).update(a["params"])


@op("plugin.move", "Move an insert to another slot", {"track": A("string", True), "plugin": A("string", True),
    "slot": A("integer", True)}, positional=["track", "plugin", "slot"])
def _plugin_move(doc, a, ctx):
    t = find_track(doc, a["track"]); p = find_insert(t, a["plugin"])
    t["inserts"].remove(p)
    t["inserts"].insert(max(0, min(int(a["slot"]), len(t["inserts"]))), p)


@op("sidechain.set", "Feed another track into an insert's sidechain (or clear with source=null)",
    {"track": A("string", True), "plugin": A("string", True), "source": A("string")}, positional=["track", "plugin", "source"])
def _sidechain(doc, a, ctx):
    t = find_track(doc, a["track"]); p = find_insert(t, a["plugin"])
    if a.get("source"):
        src = find_track(doc, a["source"])
        if src is t:
            raise OpError("a track cannot sidechain itself")
        p["sidechain"] = src["id"]
    else:
        p.pop("sidechain", None)
    from .graph import order
    order(doc)


@op("instrument.set", "Replace a track's instrument", {"track": A("string", True), "plugin": A("string", True),
    "params": A("object"), "path": A("string")}, positional=["track", "plugin"])
def _instrument_set(doc, a, ctx):
    t = find_track(doc, a["track"])
    if t["type"] != "instrument":
        raise OpError(f"{t['id']} is not an instrument track")
    _check_plugin_ref(a["plugin"], instrument=True)
    _check_params(a["plugin"], a.get("params"))
    inst = {"plugin": a["plugin"]}
    if a.get("params"):
        inst["params"] = dict(a["params"])
    if a.get("path"):
        inst["path"] = a["path"]
    t["instrument"] = inst


@op("plugin.state_save", "Store a plugin's full state blob (from a render) into states/",
    {"track": A("string", True), "plugin": A("string", desc="insert id or 'instrument'"), "data": A("bytes", True)})
def _plugin_state(doc, a, ctx):
    t = find_track(doc, a["track"])
    p = t["instrument"] if a.get("plugin") in (None, "instrument") else find_insert(t, a["plugin"])
    key = f"{t['id']}_{'instrument' if p is t.get('instrument') else p['id']}"
    d = Path(ctx.project_dir) / "states"
    d.mkdir(exist_ok=True)
    import hashlib
    data = a["data"]
    h = hashlib.sha256(data).hexdigest()[:10]
    fn = f"states/{key}_{h}.bin"
    (Path(ctx.project_dir) / fn).write_bytes(data)
    p["state"] = fn
    return {"state": fn}


# ============================================================ patterns / notes
def _norm_note(n: dict) -> dict:
    if not isinstance(n, dict):
        raise OpError(f"note must be an object, got {n!r}")
    try:
        key = note_number(n.get("key", n.get("note")))
    except Exception as e:
        raise OpError(str(e))
    t = _num(n.get("t", 0), "t", 0)
    ln = _num(n.get("len", 1), "len", 1e-4)
    vel = _num(n.get("vel", 100), "vel", 1, 127)
    out = {"t": round(t, 6), "len": round(ln, 6), "key": key, "vel": int(round(vel))}
    return out


def _sort_notes(p):
    p["notes"].sort(key=lambda n: (n["t"], n["key"]))


@op("pattern.add", "Create a MIDI pattern", {"id": A("string"), "name": A("string"), "lengthBeats": A("number"),
    "notes": A("array")}, positional=["id"])
def _pattern_add(doc, a, ctx):
    ids = set(doc["patterns"])
    pid = a.get("id") or unique_id(ids, a.get("name") or "pattern")
    if pid in ids:
        raise OpError(f"id {pid!r} already used")
    p = {"lengthBeats": _num(a.get("lengthBeats", 16), "lengthBeats", 0.25, 4096), "notes": []}
    if a.get("name"):
        p["name"] = a["name"]
    p["notes"] = [_norm_note(n) for n in a.get("notes") or []]
    _sort_notes(p)
    doc["patterns"][pid] = p
    return {"id": pid}


@op("pattern.remove", "Delete a pattern (fails if clips use it unless force)", {"pattern": A("string", True),
    "force": A("boolean")}, destructive=True, positional=["pattern"])
def _pattern_remove(doc, a, ctx):
    find_pattern(doc, a["pattern"])
    users = [(t["id"], c["id"]) for t in doc["tracks"] for c in t.get("clips", []) if c.get("pattern") == a["pattern"]]
    if users and not a.get("force"):
        raise OpError(f"pattern used by clips {users}; pass force=true to delete them too")
    for t in doc["tracks"]:
        if "clips" in t:
            t["clips"] = [c for c in t["clips"] if c.get("pattern") != a["pattern"]]
    del doc["patterns"][a["pattern"]]


@op("pattern.duplicate", "Copy a pattern", {"pattern": A("string", True), "id": A("string")}, positional=["pattern"])
def _pattern_dup(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    ids = set(doc["patterns"])
    pid = a.get("id") or unique_id(ids, a["pattern"] + "_copy")
    if pid in ids:
        raise OpError(f"id {pid!r} already used")
    doc["patterns"][pid] = copy.deepcopy(p)
    return {"id": pid}


@op("pattern.set_length", "Set pattern length in quarter notes", {"pattern": A("string", True),
    "lengthBeats": A("number", True)}, positional=["pattern", "lengthBeats"])
def _pattern_len(doc, a, ctx):
    find_pattern(doc, a["pattern"])["lengthBeats"] = _num(a["lengthBeats"], "lengthBeats", 0.25, 4096)


@op("pattern.add_notes", "Add notes {t, len, key|note, vel} (t/len in quarter notes, key 60=C4 or 'C4')",
    {"pattern": A("string", True), "notes": A("array", True)}, positional=["pattern"])
def _pattern_add_notes(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    if not isinstance(a["notes"], list) or not a["notes"]:
        raise OpError("notes must be a non-empty list")
    new = [_norm_note(n) for n in a["notes"]]
    p["notes"].extend(new)
    _sort_notes(p)
    return {"added": len(new)}


def _select(p, a):
    idx = a.get("indices")
    f = a.get("filter") or {}
    sel = []
    for i, n in enumerate(p["notes"]):
        if idx is not None and i not in idx:
            continue
        if f:
            if "keyMin" in f and n["key"] < note_number(f["keyMin"]): continue
            if "keyMax" in f and n["key"] > note_number(f["keyMax"]): continue
            if "key" in f and n["key"] != note_number(f["key"]): continue
            if "tMin" in f and n["t"] < float(f["tMin"]): continue
            if "tMax" in f and n["t"] >= float(f["tMax"]): continue
        sel.append(i)
    if idx is None and not f and not a.get("all"):
        raise OpError("select notes with indices, filter {keyMin,keyMax,key,tMin,tMax} or all=true")
    return sel


SEL_ARGS = {"indices": A("array"), "filter": A("object"), "all": A("boolean")}


@op("pattern.remove_notes", "Remove selected notes", {"pattern": A("string", True), **SEL_ARGS}, positional=["pattern"])
def _pattern_remove_notes(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    sel = set(_select(p, a))
    p["notes"] = [n for i, n in enumerate(p["notes"]) if i not in sel]
    return {"removed": len(sel)}


@op("pattern.edit_notes", "Change selected notes: set {vel,len}, shift {t,key}",
    {"pattern": A("string", True), **SEL_ARGS, "set": A("object"), "shift": A("object")}, positional=["pattern"])
def _pattern_edit(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    sel = _select(p, a)
    st, sh = a.get("set") or {}, a.get("shift") or {}
    for i in sel:
        n = p["notes"][i]
        if "vel" in st: n["vel"] = int(_num(st["vel"], "vel", 1, 127))
        if "len" in st: n["len"] = _num(st["len"], "len", 1e-4)
        if "t" in sh: n["t"] = round(max(0.0, n["t"] + _num(sh["t"], "shift.t")), 6)
        if "key" in sh:
            k = n["key"] + int(sh["key"])
            if not 0 <= k <= 127:
                raise OpError("shift moves a note out of 0..127")
            n["key"] = k
    _sort_notes(p)
    return {"changed": len(sel)}


@op("pattern.transpose", "Transpose all notes", {"pattern": A("string", True), "semitones": A("integer", True)},
    positional=["pattern", "semitones"])
def _pattern_transpose(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    s = int(a["semitones"])
    for n in p["notes"]:
        if not 0 <= n["key"] + s <= 127:
            raise OpError("transpose moves a note out of range")
    for n in p["notes"]:
        n["key"] += s


@op("pattern.quantize", "Quantize note starts (grid in quarter notes, 0.25 = 1/16)",
    {"pattern": A("string", True), "grid": A("number"), "strength": A("number"), "lengths": A("boolean")},
    positional=["pattern"])
def _pattern_quant(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    g = _num(a.get("grid", 0.25), "grid", 1 / 64, 16)
    s = _num(a.get("strength", 1.0), "strength", 0, 1)
    for n in p["notes"]:
        q = round(n["t"] / g) * g
        n["t"] = round(n["t"] + (q - n["t"]) * s, 6)
        if a.get("lengths"):
            n["len"] = round(max(g, round(n["len"] / g) * g), 6)
    _sort_notes(p)


@op("pattern.humanize", "Randomise timing/velocity slightly (seeded, repeatable)",
    {"pattern": A("string", True), "timing": A("number"), "velocity": A("number"), "seed": A("integer")},
    positional=["pattern"])
def _pattern_human(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    rng = np.random.default_rng(int(a.get("seed", 1)))
    tj, vj = _num(a.get("timing", 0.01), "timing", 0, 0.25), _num(a.get("velocity", 6), "velocity", 0, 64)
    for n in p["notes"]:
        n["t"] = round(max(0.0, n["t"] + float(rng.uniform(-tj, tj))), 6)
        n["vel"] = int(np.clip(round(n["vel"] + float(rng.uniform(-vj, vj))), 1, 127))
    _sort_notes(p)


@op("pattern.ramp_velocity", "Ramp velocities from->to across the pattern (snare rolls, builds)",
    {"pattern": A("string", True), "from": A("number", True), "to": A("number", True)},
    positional=["pattern", "from", "to"])
def _pattern_ramp(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    v0, v1 = _num(a["from"], "from", 1, 127), _num(a["to"], "to", 1, 127)
    L = p["lengthBeats"]
    for n in p["notes"]:
        n["vel"] = int(round(v0 + (v1 - v0) * min(1.0, n["t"] / L)))


@op("pattern.fill", "Fill a pattern with a repeating note (e.g. four-on-the-floor kick, offbeat hats)",
    {"pattern": A("string", True), "key": A("string", True), "every": A("number", True), "offset": A("number"),
     "len": A("number"), "vel": A("number"), "start": A("number"), "end": A("number")},
    positional=["pattern", "key", "every"])
def _pattern_fill(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    ev = _num(a["every"], "every", 1 / 64, 64)
    off = _num(a.get("offset", 0), "offset", 0)
    end = _num(a.get("end", p["lengthBeats"]), "end", 0)
    t = _num(a.get("start", 0), "start", 0) + off
    k = note_number(a["key"])
    added = 0
    while t < end - 1e-9:
        p["notes"].append({"t": round(t, 6), "len": _num(a.get("len", min(ev, 0.25)), "len", 1e-4), "key": k,
                           "vel": int(_num(a.get("vel", 100), "vel", 1, 127))})
        t += ev; added += 1
    _sort_notes(p)
    return {"added": added}


@op("pattern.set_cc", "Set a MIDI CC lane for external plugins: points [[beat, 0..127], ...]",
    {"pattern": A("string", True), "cc": A("integer", True), "points": A("array", True)}, positional=["pattern", "cc"])
def _pattern_cc(doc, a, ctx):
    p = find_pattern(doc, a["pattern"])
    cc = int(a["cc"])
    if not 0 <= cc <= 127:
        raise OpError("cc 0..127")
    pts = [[_num(x[0], "beat", 0), int(_num(x[1], "value", 0, 127))] for x in a["points"]]
    p.setdefault("cc", {})[str(cc)] = sorted(pts)


# ============================================================ clips
def _clip_id(doc, t, base=None):
    ids = all_ids(doc)
    if base:
        return unique_id(ids, base)
    i = len(t.get("clips", [])) + 1
    while f"{t['id']}_c{i}" in ids:
        i += 1
    return f"{t['id']}_c{i}"


@op("clip.add", "Place an audio file on an audio track (imports into media/)",
    {"track": A("string", True), "file": A("string", True), "at": A("position", True), "lengthBeats": A("number"),
     "offsetSec": A("number"), "gainDb": A("number"), "fadeIn": A("number"), "fadeOut": A("number"),
     "stretch": A("object"), "pitch": A("number"), "reverse": A("boolean"), "loop": A("boolean"), "id": A("string")},
    positional=["track", "file", "at"])
def _clip_add(doc, a, ctx):
    t = find_track(doc, a["track"])
    if t["type"] != "audio":
        raise OpError(f"{t['id']} is not an audio track (patterns go on instrument tracks: clip.add_pattern)")
    _pos(doc, a["at"])
    f = str(a["file"])
    from .media import import_file
    pd = Path(ctx.project_dir)
    if not (f.startswith("media/") and (pd / f).exists()):
        src = Path(f)
        if not src.is_absolute():
            src = (pd / f) if (pd / f).exists() else Path.cwd() / f
        f = import_file(pd, src)
    c = {"id": a.get("id") or _clip_id(doc, t), "file": f, "at": a["at"]}
    for k in ("lengthBeats", "offsetSec", "gainDb", "fadeIn", "fadeOut", "pitch"):
        if k in a:
            c[k] = _num(a[k], k)
    if "stretch" in a:
        c["stretch"] = _check_stretch(a["stretch"])
    for k in ("reverse", "loop"):
        if a.get(k):
            c[k] = True
    t["clips"].append(c)
    return {"id": c["id"], "file": f}


def _check_stretch(st):
    if not isinstance(st, dict) or st.get("mode") not in ("off", "tempo", "ratio", "varispeed"):
        raise OpError("stretch must be {mode: off|tempo|ratio|varispeed, sourceBpm?, ratio?}")
    if st["mode"] == "tempo":
        _num(st.get("sourceBpm"), "stretch.sourceBpm", 20, 999)
    if st["mode"] in ("ratio", "varispeed"):
        _num(st.get("ratio", 1), "stretch.ratio", 0.05, 20)
    return dict(st)


@op("clip.add_pattern", "Place a pattern on an instrument track",
    {"track": A("string", True), "pattern": A("string", True), "at": A("position", True), "repeat": A("integer"),
     "lengthBeats": A("number"), "transpose": A("integer"), "velocity": A("number"), "id": A("string")},
    positional=["track", "pattern", "at"])
def _clip_add_pattern(doc, a, ctx):
    t = find_track(doc, a["track"])
    if t["type"] != "instrument":
        raise OpError(f"{t['id']} is not an instrument track")
    find_pattern(doc, a["pattern"])
    _pos(doc, a["at"])
    c = {"id": a.get("id") or _clip_id(doc, t), "pattern": a["pattern"], "at": a["at"]}
    if int(a.get("repeat", 1)) != 1:
        c["repeat"] = int(_num(a["repeat"], "repeat", 1, 4096))
    for k in ("lengthBeats", "velocity"):
        if k in a:
            c[k] = _num(a[k], k, 0.01)
    if a.get("transpose"):
        c["transpose"] = int(a["transpose"])
    t["clips"].append(c)
    return {"id": c["id"]}


@op("clip.add_notes", "Write notes straight onto an instrument track (creates a pattern + clip)",
    {"track": A("string", True), "at": A("position", True), "notes": A("array", True), "lengthBeats": A("number"),
     "pattern": A("string"), "repeat": A("integer")}, positional=["track", "at"])
def _clip_add_notes(doc, a, ctx):
    t = find_track(doc, a["track"])
    notes = [_norm_note(n) for n in a["notes"]]
    if not notes:
        raise OpError("notes must be a non-empty list")
    L = a.get("lengthBeats") or max(4.0, math.ceil(max(n["t"] + n["len"] for n in notes) / 4) * 4)
    r = _pattern_add(doc, {"id": a.get("pattern"), "name": f"{t['name']} notes", "lengthBeats": L, "notes": notes}, ctx)
    r2 = _clip_add_pattern(doc, {"track": t["id"], "pattern": r["id"], "at": a["at"], "repeat": a.get("repeat", 1)}, ctx)
    return {"pattern": r["id"], "clip": r2["id"], "added": len(notes)}


@op("clip.remove", "Delete a clip", {"track": A("string", True), "clip": A("string", True)}, positional=["track", "clip"])
def _clip_remove(doc, a, ctx):
    t = find_track(doc, a["track"]); t["clips"].remove(find_clip(t, a["clip"]))


@op("clip.move", "Move a clip to a position (at), by quarter notes (by), and/or to another track",
    {"track": A("string", True), "clip": A("string", True), "at": A("position"), "by": A("number"),
     "toTrack": A("string")}, positional=["track", "clip"])
def _clip_move(doc, a, ctx):
    t = find_track(doc, a["track"]); c = find_clip(t, a["clip"])
    tm = TimeMap(doc["transport"])
    if "at" in a:
        _pos(doc, a["at"]); c["at"] = a["at"]
    if "by" in a:
        q = tm.qn(c["at"]) + _num(a["by"], "by")
        if q < 0:
            raise OpError("clip would start before the song")
        c["at"] = tm.pos(q)
    if a.get("toTrack"):
        dst = find_track(doc, a["toTrack"])
        if dst["type"] != t["type"]:
            raise OpError("can only move clips between tracks of the same type")
        t["clips"].remove(c); dst["clips"].append(c)
    if not any(k in a for k in ("at", "by", "toTrack")):
        raise OpError("give at, by or toTrack")


@op("clip.set", "Change clip fields (gainDb, fades, pitch, stretch, reverse, loop, offsetSec, lengthBeats, repeat, transpose, velocity, mute)",
    {"track": A("string", True), "clip": A("string", True), "gainDb": A("number"), "fadeIn": A("number"),
     "fadeOut": A("number"), "pitch": A("number"), "stretch": A("object"), "reverse": A("boolean"),
     "loop": A("boolean"), "offsetSec": A("number"), "lengthBeats": A("number"), "repeat": A("integer"),
     "transpose": A("integer"), "velocity": A("number"), "mute": A("boolean")}, positional=["track", "clip"])
def _clip_set(doc, a, ctx):
    t = find_track(doc, a["track"]); c = find_clip(t, a["clip"])
    changed = 0
    for k in ("gainDb", "fadeIn", "fadeOut", "pitch", "offsetSec", "lengthBeats", "velocity"):
        if k in a:
            c[k] = _num(a[k], k); changed += 1
    for k in ("repeat", "transpose"):
        if k in a:
            c[k] = int(a[k]); changed += 1
    if "stretch" in a:
        c["stretch"] = _check_stretch(a["stretch"]); changed += 1
    for k in ("reverse", "loop", "mute"):
        if k in a:
            if a[k]:
                c[k] = True
            else:
                c.pop(k, None)
            changed += 1
    if not changed:
        raise OpError("nothing to change")


@op("clip.split", "Split an audio clip (needs lengthBeats) or a pattern clip at a position",
    {"track": A("string", True), "clip": A("string", True), "at": A("position", True)}, positional=["track", "clip", "at"])
def _clip_split(doc, a, ctx):
    t = find_track(doc, a["track"]); c = find_clip(t, a["clip"])
    tm = TimeMap(doc["transport"])
    s, cut = tm.qn(c["at"]), tm.qn(a["at"])
    if "pattern" in c:
        L = find_pattern(doc, c["pattern"])["lengthBeats"]
        total = c.get("lengthBeats") or L * c.get("repeat", 1)
        if not s < cut < s + total:
            raise OpError("split point outside the clip")
        rel = cut - s
        if abs(rel / L - round(rel / L)) > 1e-9:
            raise OpError("pattern clips split on repeat boundaries; use clip.make_unique and edit notes otherwise")
        first_reps = int(round(rel / L))
        c2 = copy.deepcopy(c)
        c["repeat"] = first_reps
        c.pop("lengthBeats", None)
        c2["id"] = _clip_id(doc, t); c2["at"] = a["at"]
        c2["repeat"] = int(round(total / L)) - first_reps
        t["clips"].append(c2)
        return {"id": c2["id"]}
    if not c.get("lengthBeats"):
        raise OpError("set lengthBeats on the audio clip before splitting")
    if not s < cut < s + c["lengthBeats"]:
        raise OpError("split point outside the clip")
    st = c.get("stretch") or {}
    speed = 1.0
    if st.get("mode") == "tempo":
        speed = tm.bpm_at(s) / float(st["sourceBpm"])
    elif st.get("mode") in ("ratio", "varispeed"):
        speed = float(st.get("ratio", 1.0))
    c2 = copy.deepcopy(c)
    c2["id"] = _clip_id(doc, t)
    c2["at"] = a["at"]
    c2["lengthBeats"] = round(s + c["lengthBeats"] - cut, 6)
    c2["offsetSec"] = round(c.get("offsetSec", 0.0) + (tm.sec(cut) - tm.sec(s)) * speed, 6)
    c2.pop("fadeIn", None)
    c["lengthBeats"] = round(cut - s, 6)
    c.pop("fadeOut", None)
    t["clips"].append(c2)
    return {"id": c2["id"]}


@op("clip.duplicate", "Copy a clip to a position (default: right after it)",
    {"track": A("string", True), "clip": A("string", True), "at": A("position")}, positional=["track", "clip"])
def _clip_dup(doc, a, ctx):
    t = find_track(doc, a["track"]); c = find_clip(t, a["clip"])
    tm = TimeMap(doc["transport"])
    c2 = copy.deepcopy(c); c2["id"] = _clip_id(doc, t)
    if a.get("at"):
        c2["at"] = _pos(doc, a["at"])
    else:
        if "pattern" in c:
            L = c.get("lengthBeats") or find_pattern(doc, c["pattern"])["lengthBeats"] * c.get("repeat", 1)
        elif c.get("lengthBeats"):
            L = c["lengthBeats"]
        else:
            raise OpError("give at= for audio clips without lengthBeats")
        c2["at"] = tm.pos(tm.qn(c["at"]) + L)
    t["clips"].append(c2)
    return {"id": c2["id"]}


@op("clip.make_unique", "Give a pattern clip its own copy of the pattern", {"track": A("string", True),
    "clip": A("string", True)}, positional=["track", "clip"])
def _clip_unique(doc, a, ctx):
    t = find_track(doc, a["track"]); c = find_clip(t, a["clip"])
    if "pattern" not in c:
        raise OpError("not a pattern clip")
    r = _pattern_dup(doc, {"pattern": c["pattern"]}, ctx)
    c["pattern"] = r["id"]
    return {"pattern": r["id"]}


# ============================================================ automation
def _check_target(t, target):
    if target in ("mix/gainDb", "mix/pan"):
        return
    parts = target.split("/")
    if parts[0] == "sends" and len(parts) == 3 and parts[2] == "levelDb":
        if not any(s["to"] == parts[1] for s in t.get("sends", [])):
            raise OpError(f"{t['id']} has no send to {parts[1]}")
        return
    if parts[0] == "inserts" and len(parts) == 3:
        p = find_insert(t, parts[1])
        fmt, _, name = p["plugin"].partition(":")
        if fmt == "builtin" and parts[2] not in dsp.PARAMS[name].get("_auto", []):
            raise OpError(f"builtin:{name} automatable params: {', '.join(dsp.PARAMS[name].get('_auto', [])) or 'none'}")
        return
    if parts[0] == "instrument" and len(parts) == 2:
        inst = t.get("instrument")
        if not inst or inst["plugin"].startswith("builtin:"):
            raise OpError("instrument automation needs a CLAP/VST3 instrument")
        return
    raise OpError("target must be mix/gainDb, mix/pan, sends/<to>/levelDb, inserts/<id>/<param> or instrument/<param>")


def _points(doc, pts):
    if not isinstance(pts, list) or not pts:
        raise OpError("points must be a non-empty list of [position, value]")
    out = []
    for p in pts:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise OpError(f"bad point {p!r}")
        _pos(doc, p[0], "point")
        out.append([p[0], _num(p[1], "value")])
    tm = TimeMap(doc["transport"])
    out.sort(key=lambda x: tm.qn(x[0]))
    return out


def _canon_target(doc, t, target):
    parts = target.split("/")
    if parts[0] == "inserts" and len(parts) == 3:
        parts[1] = find_insert(t, parts[1])["id"]
    if parts[0] == "sends" and len(parts) == 3:
        parts[1] = find_track(doc, parts[1])["id"]
    return "/".join(parts)


@op("auto.set", "Create/replace an automation lane: points [[position, value], ...]",
    {"track": A("string", True), "target": A("string", True), "points": A("array", True), "curve": A("string")},
    positional=["track", "target"])
def _auto_set(doc, a, ctx):
    t = find_track(doc, a["track"])
    target = _canon_target(doc, t, a["target"])
    _check_target(t, target)
    pts = _points(doc, a["points"])
    lanes = t.setdefault("automation", [])
    lane = next((l for l in lanes if l["target"] == target), None)
    if lane is None:
        lane = {"target": target}; lanes.append(lane)
    lane["points"] = pts
    if a.get("curve"):
        if a["curve"] not in ("linear", "hold"):
            raise OpError("curve: linear|hold")
        lane["curve"] = a["curve"]


@op("auto.add_points", "Add points to an automation lane", {"track": A("string", True), "target": A("string", True),
    "points": A("array", True)}, positional=["track", "target"])
def _auto_add(doc, a, ctx):
    t = find_track(doc, a["track"])
    target = _canon_target(doc, t, a["target"])
    _check_target(t, target)
    lane = next((l for l in t.setdefault("automation", []) if l["target"] == target), None)
    pts = _points(doc, a["points"])
    if lane is None:
        t["automation"].append({"target": target, "points": pts})
    else:
        lane["points"] = _points(doc, lane["points"] + pts)


@op("auto.clear", "Remove an automation lane", {"track": A("string", True), "target": A("string", True)},
    positional=["track", "target"])
def _auto_clear(doc, a, ctx):
    t = find_track(doc, a["track"])
    target = _canon_target(doc, t, a["target"])
    n = len(t.get("automation", []))
    t["automation"] = [l for l in t.get("automation", []) if l["target"] != target]
    if len(t["automation"]) == n:
        raise OpError(f"no automation lane {target}")


@op("auto.shape", "Draw a shape into a lane between start and end (ramp, exp, sine, saw, square)",
    {"track": A("string", True), "target": A("string", True), "start": A("position", True), "end": A("position", True),
     "from": A("number", True), "to": A("number", True), "shape": A("string"), "rateBeats": A("number"),
     "steps": A("integer")}, positional=["track", "target"])
def _auto_shape(doc, a, ctx):
    tm = TimeMap(doc["transport"])
    s, e = tm.qn(a["start"]), tm.qn(a["end"])
    if e <= s:
        raise OpError("end must be after start")
    v0, v1 = _num(a["from"], "from"), _num(a["to"], "to")
    shape = a.get("shape", "ramp")
    steps = int(a.get("steps", 64))
    rate = _num(a.get("rateBeats", 1.0), "rateBeats", 1 / 32)
    pts = []
    for i in range(steps + 1):
        u = i / steps
        q = s + (e - s) * u
        if shape == "ramp":
            v = v0 + (v1 - v0) * u
        elif shape == "exp":
            v = v0 + (v1 - v0) * (u ** 3)
        elif shape in ("sine", "saw", "square"):
            ph = ((q - s) / rate) % 1.0
            w = {"sine": 0.5 - 0.5 * math.cos(2 * math.pi * ph), "saw": ph, "square": 1.0 if ph < 0.5 else 0.0}[shape]
            v = v0 + (v1 - v0) * w
        else:
            raise OpError("shape: ramp|exp|sine|saw|square")
        pts.append([round(q, 6), round(v, 6)])
    t = find_track(doc, a["track"])
    target = _canon_target(doc, t, a["target"])
    _check_target(t, target)
    lane = next((l for l in t.setdefault("automation", []) if l["target"] == target), None)
    if lane is None:
        t["automation"].append({"target": target, "points": pts})
    else:
        keep = [p for p in lane["points"] if not (s - 1e-9 <= tm.qn(p[0]) <= e + 1e-9)]
        lane["points"] = _points(doc, keep + pts)


# ============================================================ media / locks / batch
@op("media.import", "Copy an audio file into the project media store", {"path": A("string", True)}, positional=["path"])
def _media_import(doc, a, ctx):
    from .media import import_file
    return {"file": import_file(Path(ctx.project_dir), Path(a["path"]))}


@op("lock.set", "Protect a track or pattern from edits by the AI (or by everyone)",
    {"target": A("string", True, "track id or pattern:<id>"), "against": A("string")}, positional=["target"])
def _lock_set(doc, a, ctx):
    against = a.get("against", "ai")
    if against not in ("ai", "all"):
        raise OpError("against: ai|all")
    tgt = a["target"]
    if tgt.startswith("pattern:"):
        find_pattern(doc, tgt.split(":", 1)[1])
    else:
        tgt = "track:" + find_track(doc, tgt.split(":", 1)[-1])["id"]
    locks = doc.setdefault("locks", [])
    locks[:] = [l for l in locks if l["target"] != tgt]
    locks.append({"target": tgt, "against": against, "by": ctx.author})


@op("lock.clear", "Remove a lock", {"target": A("string", True)}, positional=["target"])
def _lock_clear(doc, a, ctx):
    tgt = a["target"]
    if not tgt.startswith(("track:", "pattern:")):
        tgt = "track:" + find_track(doc, tgt)["id"]
    n = len(doc.get("locks", []))
    doc["locks"] = [l for l in doc.get("locks", []) if l["target"] != tgt]
    if len(doc["locks"]) == n:
        raise OpError(f"no lock on {tgt}")
    if not doc["locks"]:
        doc.pop("locks")


@op("batch", "Run several ops as one undo step: ops [{op, args}, ...]", {"ops": A("array", True)})
def _batch(doc, a, ctx):
    results = []
    for i, item in enumerate(a["ops"]):
        name = item.get("op")
        if name == "batch" or name not in REGISTRY:
            raise OpError(f"batch item {i}: unknown op {name!r}")
        spec = REGISTRY[name]
        args = validate_args(spec, item.get("args") or {})
        try:
            results.append({"op": name, **(spec.fn(doc, args, ctx) or {})})
        except (OpError, ModelError, ValueError, KeyError, TypeError) as e:
            raise OpError(f"batch item {i} ({name}): {e}")
    return {"results": results}


def apply_op(doc: dict, name: str, args: dict, ctx: Ctx) -> tuple[dict, dict]:
    """Run op `name` on a copy of doc. Returns (new_doc, result)."""
    if name not in REGISTRY:
        raise OpError(f"unknown op {name!r}. Try ops.list")
    spec = REGISTRY[name]
    args = validate_args(spec, args or {})
    new = copy.deepcopy(doc)
    try:
        res = spec.fn(new, args, ctx) or {}
    except OpError:
        raise
    except (ModelError, ValueError) as e:
        raise OpError(str(e))
    except (KeyError, TypeError) as e:
        raise OpError(f"{name}: bad arguments ({e})")
    from .model import validate
    errs = validate(new)
    if errs:
        raise OpError(f"{name} would make the project invalid: {'; '.join(errs[:5])}")
    return new, res


def op_catalog() -> list[dict]:
    return [{"op": s.name, "summary": s.summary, "destructive": s.destructive, "positional": s.positional,
             "args": {k: {"type": v[0], "required": v[1], **({"desc": v[2]} if v[2] else {})} for k, v in s.args.items()}}
            for s in REGISTRY.values()]
