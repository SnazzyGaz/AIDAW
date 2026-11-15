"""Project model helpers: new projects, canonical JSON, lookup, validation."""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any

from . import FORMAT
from .timebase import TimeMap, note_number

KEY_ORDER = ["format", "meta", "settings", "transport", "patterns", "tracks", "markers", "locks",
             "id", "type", "name", "title", "author", "color", "role", "plugin", "path", "params", "state",
             "instrument", "clips", "inserts", "mix", "sends", "output", "automation",
             "pattern", "file", "at", "lengthBeats", "repeat", "transpose", "velocity", "offsetSec",
             "gainDb", "pan", "mute", "solo", "fadeIn", "fadeOut", "reverse", "pitch", "stretch", "loop",
             "t", "len", "key", "vel", "to", "levelDb", "pre", "target", "points", "curve", "bypass", "sidechain",
             "sampleRate", "seed", "block", "tailSec", "pluginSandbox", "tempo", "timeSig", "keySig", "loop",
             "bpm", "num", "den", "start", "end", "notes", "cc", "mode", "sourceBpm", "ratio"]
_RANK = {k: i for i, k in enumerate(KEY_ORDER)}
TRACK_TYPES = ("audio", "instrument", "bus", "return", "master")
ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


class ModelError(ValueError):
    pass


def new_project(title: str = "Untitled", bpm: float = 120, num: int = 4, den: int = 4,
                sample_rate: int = 48000, author: str = "") -> dict:
    return {
        "format": FORMAT,
        "meta": {"title": title, "author": author},
        "settings": {"sampleRate": sample_rate, "seed": 1, "block": 512, "tailSec": 2.0, "pluginSandbox": True},
        "transport": {"tempo": [{"at": "1.1.1", "bpm": bpm}],
                      "timeSig": [{"at": "1.1.1", "num": num, "den": den}],
                      "loop": {"start": "1.1.1", "end": "5.1.1"}},
        "patterns": {},
        "tracks": [{"id": "master", "type": "master", "name": "Master", "inserts": [],
                    "mix": {"gainDb": 0.0, "pan": 0.0}}],
        "markers": [],
    }


# ---------- canonical JSON ----------
def _norm(v: Any) -> Any:
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ModelError("NaN/inf not allowed in project")
        r = round(v, 6)
        return 0.0 if r == 0 else r
    if isinstance(v, dict):
        keys = sorted(v.keys(), key=lambda k: (_RANK.get(k, 10_000), str(k)))
        return {k: _norm(v[k]) for k in keys}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def _is_leafy(v) -> bool:
    if isinstance(v, dict):
        return all(not isinstance(x, (dict, list)) or (isinstance(x, list) and all(not isinstance(y, (dict, list)) for y in x)) for x in v.values())
    if isinstance(v, list):
        if len(v) == 1 and isinstance(v[0], dict) and _is_leafy(v[0]):
            return True
        return all(not isinstance(x, (dict, list)) or (isinstance(x, list) and all(not isinstance(y, (dict, list)) for y in x)) for x in v)
    return True


def _fmt(v, indent: int) -> str:
    inline = json.dumps(v, ensure_ascii=False, separators=(", ", ": "))
    if not isinstance(v, (dict, list)) or ((len(inline) + indent <= 100) and _is_leafy(v)):
        return inline
    pad = " " * (indent + 2)
    if isinstance(v, dict):
        if not v:
            return "{}"
        items = [f"{pad}{json.dumps(k, ensure_ascii=False)}: {_fmt(x, indent + 2)}" for k, x in v.items()]
        return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"
    if not v:
        return "[]"
    items = [pad + _fmt(x, indent + 2) for x in v]
    return "[\n" + ",\n".join(items) + "\n" + " " * indent + "]"


def dumps(doc: dict) -> str:
    return _fmt(_norm(doc), 0) + "\n"


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------- lookup ----------
def track(doc: dict, tid: str) -> dict:
    for t in doc["tracks"]:
        if t["id"] == tid:
            return t
    for t in doc["tracks"]:  # allow name lookup (case-insensitive)
        if t.get("name", "").lower() == str(tid).lower():
            return t
    raise ModelError(f"no track {tid!r} (tracks: {', '.join(t['id'] for t in doc['tracks'])})")


def track_index(doc: dict, tid: str) -> int:
    t = track(doc, tid)
    return doc["tracks"].index(t)


def clip(t: dict, cid: str) -> dict:
    for c in t.get("clips", []):
        if c.get("id") == cid:
            return c
    if isinstance(cid, int) or (isinstance(cid, str) and cid.isdigit()):
        i = int(cid)
        if 0 <= i < len(t.get("clips", [])):
            return t["clips"][i]
    raise ModelError(f"no clip {cid!r} on track {t['id']!r}")


def insert(t: dict, pid: str) -> dict:
    for p in t.get("inserts", []):
        if p.get("id") == pid:
            return p
    for p in t.get("inserts", []):
        if p.get("plugin") == pid or p.get("plugin", "").split(":", 1)[-1].lower() == str(pid).lower():
            return p
    if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()):
        i = int(pid)
        if 0 <= i < len(t.get("inserts", [])):
            return t["inserts"][i]
    raise ModelError(f"no plugin {pid!r} on track {t['id']!r}")


def pattern(doc: dict, pid: str) -> dict:
    p = doc.get("patterns", {}).get(pid)
    if p is None:
        raise ModelError(f"no pattern {pid!r}")
    return p


def unique_id(existing, base: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", base.strip().lower()).strip("_") or "x"
    base = base[:40]
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def all_ids(doc: dict) -> set[str]:
    ids = {t["id"] for t in doc["tracks"]}
    for t in doc["tracks"]:
        ids.update(c.get("id") for c in t.get("clips", []) if c.get("id"))
        ids.update(p.get("id") for p in t.get("inserts", []) if p.get("id"))
    return ids


def normalize(doc: dict) -> dict:
    """Fill defaults and missing ids (deterministically). Returns a new doc."""
    d = copy.deepcopy(doc)
    d.setdefault("patterns", {})
    d.setdefault("markers", [])
    s = d.setdefault("settings", {})
    s.setdefault("sampleRate", 48000); s.setdefault("seed", 1); s.setdefault("block", 512)
    s.setdefault("tailSec", 2.0); s.setdefault("pluginSandbox", True)
    if not any(t.get("type") == "master" for t in d.get("tracks", [])):
        d.setdefault("tracks", []).append({"id": "master", "type": "master", "name": "Master", "inserts": [],
                                           "mix": {"gainDb": 0.0, "pan": 0.0}})
    ids = all_ids(d)
    for t in d["tracks"]:
        t.setdefault("name", t["id"])
        t.setdefault("mix", {}).setdefault("gainDb", 0.0)
        t["mix"].setdefault("pan", 0.0)
        if t["type"] != "master":
            t.setdefault("output", "master")
        if t["type"] in ("audio", "instrument"):
            t.setdefault("clips", [])
        for n, c in enumerate(t.get("clips", [])):
            if not c.get("id"):
                c["id"] = unique_id(ids, f"{t['id']}_c{n + 1}")
                ids.add(c["id"])
        for n, p in enumerate(t.get("inserts", [])):
            if not p.get("id"):
                p["id"] = unique_id(ids, f"{t['id']}_fx{n + 1}")
                ids.add(p["id"])
    return d


def validate(doc: dict) -> list[str]:
    """Semantic checks beyond the JSON schema. Returns a list of problems."""
    errs: list[str] = []
    try:
        tm = TimeMap(doc.get("transport", {}))
    except Exception as e:
        return [f"transport: {e}"]
    ids = [t.get("id") for t in doc.get("tracks", [])]
    if len(ids) != len(set(ids)):
        errs.append("duplicate track ids")
    masters = [t for t in doc.get("tracks", []) if t.get("type") == "master"]
    if len(masters) != 1:
        errs.append("exactly one master track required")
    tids = set(ids)
    for t in doc.get("tracks", []):
        tid = t.get("id")
        if t.get("type") not in TRACK_TYPES:
            errs.append(f"track {tid}: bad type {t.get('type')!r}")
        if not isinstance(tid, str) or not ID_RE.match(tid):
            errs.append(f"track {tid!r}: bad id")
        out = t.get("output")
        if t.get("type") != "master":
            if out not in tids:
                errs.append(f"track {tid}: output {out!r} does not exist")
            else:
                ot = next(x for x in doc["tracks"] if x["id"] == out)
                if ot["type"] not in ("bus", "master", "return"):
                    errs.append(f"track {tid}: output {out!r} is not a bus/return/master")
        for s in t.get("sends", []):
            if s.get("to") not in tids:
                errs.append(f"track {tid}: send to missing track {s.get('to')!r}")
        for c in t.get("clips", []):
            try:
                tm.qn(c.get("at", 0))
            except Exception as e:
                errs.append(f"track {tid} clip {c.get('id')}: {e}")
            if "pattern" in c and c["pattern"] not in doc.get("patterns", {}):
                errs.append(f"track {tid} clip {c.get('id')}: missing pattern {c['pattern']!r}")
            if "pattern" in c and t.get("type") != "instrument":
                errs.append(f"track {tid}: pattern clips need an instrument track")
            if "file" in c and t.get("type") != "audio":
                errs.append(f"track {tid}: audio clips need an audio track")
        for p in t.get("inserts", []):
            sc = p.get("sidechain")
            if sc and sc not in tids:
                errs.append(f"track {tid} plugin {p.get('id')}: sidechain source {sc!r} missing")
        for a in t.get("automation", []):
            for pt in a.get("points", []):
                try:
                    tm.qn(pt[0]); float(pt[1])
                except Exception as e:
                    errs.append(f"track {tid} automation {a.get('target')}: {e}")
    seen = set(ids)
    for t in doc.get("tracks", []):
        for c in t.get("clips", []) + t.get("inserts", []):
            cid = c.get("id")
            if not isinstance(cid, str) or not ID_RE.match(cid):
                errs.append(f"track {t.get('id')}: bad id {cid!r} (letters, digits, _ . -)")
            elif cid in seen:
                errs.append(f"duplicate id {cid!r}")
            seen.add(cid)
    for pid, p in doc.get("patterns", {}).items():
        if not ID_RE.match(str(pid)):
            errs.append(f"bad pattern id {pid!r} (letters, digits, _ . -)")
        for i, n in enumerate(p.get("notes", [])):
            try:
                note_number(n["key"])
                if float(n["len"]) <= 0:
                    raise ValueError("length must be > 0")
                if not 0 <= float(n.get("vel", 100)) <= 127:
                    raise ValueError("velocity 0..127")
            except Exception as e:
                errs.append(f"pattern {pid} note {i}: {e}")
    for m in doc.get("markers", []):
        try:
            tm.qn(m["at"])
        except Exception as e:
            errs.append(f"marker {m.get('name')}: {e}")
    try:
        from .graph import order
        order(doc)
    except Exception as e:
        errs.append(str(e))
    return errs
