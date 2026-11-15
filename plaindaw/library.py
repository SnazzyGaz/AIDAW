"""Sound library: index presets and samples on disk, auto-tag samples with the listening
module, search, audition, and use them in a project.

Indexed: Surge XT patches (.fxp, name/category/author from the patch XML), VST3 presets
(.vstpreset, plugin class id from the header), SFZ instruments (.sfz), other synth presets
(.fxp/.fxb VST2 chunks, .vital, .h2p, .nmsv - indexed by name and folder only), and audio
samples (.wav .flac .aif .aiff .ogg .mp3), which get type / key / tempo / brightness tags.
Index file: $PLAINDAW_LIBRARY or ~/.plaindaw/library.json.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np

AUDIO = {".wav", ".flac", ".aif", ".aiff", ".ogg", ".mp3"}
PRESETS = {".fxp", ".fxb", ".vstpreset", ".vital", ".h2p", ".nmsv", ".sfz"}
TYPE_WORDS = [  # (regex on lower-case path words, type)
    (r"\b(kick|kik|bd|bassdrum)\b", "kick"), (r"\b(snare|snr|sd)\b", "snare"), (r"\bclap\b", "clap"),
    (r"\b(open ?hat|oh|openhh)\b", "openhat"), (r"\b(hat|hh|hihat|hi-hat)\b", "hat"), (r"\b(crash|cymbal)\b", "crash"),
    (r"\bride\b", "ride"), (r"\btoms?\b", "tom"), (r"\b(shaker|tamb|tambourine)\b", "shaker"), (r"\bperc\w*\b", "perc"),
    (r"\b808s?\b", "808"), (r"\b(bass|sub)\b", "bass"), (r"\b(pad|pads)\b", "pad"), (r"\b(lead|leads)\b", "lead"),
    (r"\b(pluck|plucks)\b", "pluck"), (r"\b(keys|piano|ep|rhodes)\b", "keys"), (r"\b(string|strings|violin|cello)\b", "strings"),
    (r"\b(vox|vocal|vocals|voice|chant)\b", "vocal"), (r"\b(riser|sweep|uplifter)\b", "riser"),
    (r"\b(impact|hit|boom)\b", "impact"), (r"\b(fx|sfx|foley)\b", "fx"), (r"\b(loop|loops)\b", "loop"),
    (r"\b(chord|chords|stab|stabs)\b", "chords"), (r"\b(arp|seq|sequence)\b", "arp"),
]
SYNONYMS = {"hihat": "hat", "hh": "hat", "hats": "hat", "kicks": "kick", "snares": "snare", "claps": "clap",
            "basses": "bass", "sub": "bass", "808": "808", "vox": "vocal", "vocals": "vocal", "leads": "lead",
            "pads": "pad", "plucks": "pluck", "sweep": "riser", "uplifter": "riser", "piano": "keys", "ep": "keys",
            "violin": "strings", "polysynth": "chords", "polysynths": "chords", "stab": "chords", "sequence": "arp",
            "warm": "dark", "airy": "bright", "crisp": "bright", "fat": "wide", "punch": "punchy"}
NOTE_RE = re.compile(r"(?<![a-z])([a-g])(#|b|sharp|flat)?\s?(maj|min|m|minor|major)?(?![a-z])")


def index_path() -> Path:
    return Path(os.environ.get("PLAINDAW_LIBRARY") or Path.home() / ".plaindaw" / "library.json")


def load() -> dict:
    p = index_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError:
            pass
    return {"version": 1, "roots": [], "items": {}, "vst3Classes": {}}


def save(idx: dict) -> None:
    p = index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, indent=1))
    tmp.replace(p)


def _id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:10]


def _words(path: Path) -> str:
    s = " ".join(path.parts[-4:])
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return re.sub(r"[_\-.()\[\]+]+", " ", s).lower()


def _type_from_words(w: str) -> list[str]:
    out = []
    for rx, t in TYPE_WORDS:
        if re.search(rx, w) and t not in out:
            out.append(t)
    return out


def _bpm_from_words(w: str):
    m = re.search(r"\b(\d{2,3})\s?bpm\b", w)
    return float(m.group(1)) if m and 50 <= float(m.group(1)) <= 200 else None


def _key_from_words(w: str):
    m = re.search(r"\b([a-g])(#|b|sharp|flat)?\s?(maj|major|min|minor|m)\b", w)
    if not m:
        return None
    acc = {"#": "#", "sharp": "#", "b": "b", "flat": "b"}.get(m.group(2) or "", "")
    q = "minor" if (m.group(3) or "").startswith("m") and m.group(3) not in ("maj", "major") else "major"
    return f"{m.group(1).upper()}{acc} {q}"


# ------------------------------------------------------------------ preset readers
def read_fxp(path: Path) -> dict:
    data = path.read_bytes()[:200000]
    info = {"engine": "vst2", "format": "fxp"}
    if data[:4] != b"CcnK":
        return info
    fx_id = data[16:20].decode("latin-1", "replace")
    info["fxId"] = fx_id
    if fx_id == "cjs3":  # Surge
        info["engine"] = "surge"
    m = re.search(rb"<meta\s+([^>]*)>", data)
    if m:
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1).decode("utf-8", "replace")))
        info["engine"] = "surge"
        for k in ("name", "category", "author", "comment"):
            if attrs.get(k):
                info[k] = attrs[k]
    elif data[56:60] and fx_id != "cjs3":
        name = data[28:56].split(b"\0")[0].decode("latin-1", "replace").strip()
        if name:
            info["name"] = name
    return info


def read_vstpreset(path: Path) -> dict:
    head = path.read_bytes()[:48]
    if head[:4] != b"VST3":
        return {"engine": "vst3", "format": "vstpreset", "error": "not a VST3 preset"}
    return {"engine": "vst3", "format": "vstpreset", "classId": head[8:40].decode("latin-1")}


def read_sfz(path: Path) -> dict:
    txt = path.read_text(errors="replace")
    return {"engine": "sfz", "format": "sfz", "regions": len(re.findall(r"<region>", txt))}


# ------------------------------------------------------------------ sample tagging
def tag_audio(path: Path, max_sec: float = 30.0) -> dict:
    import soundfile as sf
    from . import listen as L
    from .media import decode
    try:
        inf = sf.info(str(path))
        dur = float(inf.duration)
    except Exception:
        dur = None
    try:
        x = decode(path, L.SR)
    except Exception as e:
        return {"error": f"cannot decode: {e}"}
    x = np.asarray(x)[:, : int(max_sec * L.SR)]
    if dur is None:
        dur = x.shape[1] / L.SR
    m = L._mono22(x, L.SR)
    out = {"durationSec": round(dur, 3)}
    peak = float(np.max(np.abs(m))) if m.size else 0.0
    if peak < 1e-5:
        out["features"] = {"silent": True}
        return out
    tb = L.timbre(x, L.SR)
    tr = L.transients(m)
    feat = {"centroidHz": tb.get("centroidHz"), "flatness": tb.get("flatness"), "width": tb.get("width"),
            "transientDb": tr.get("strengthDb"), "peakDb": round(20 * math.log10(peak), 1)}
    tags = list(tb.get("tags") or [])
    env = L.onset_envelope(m)
    on = L.onsets(env)
    n_on = int(np.sum(env[on] > 0.3 * float(env.max()))) if on.size else 0   # ignore tail wiggles
    tonal = (tb.get("flatness") or 1) < 0.08
    if dur >= 2.0 and n_on >= 4:
        tp = L.estimate_tempo(env)
        if tp.get("bpm"):
            out["bpm"] = tp["bpm"]
            feat["tempoConfidence"] = tp.get("confidence")
    if tonal and dur >= 0.4:
        C = L.chroma(L._stft(m))
        if C.size and C.sum() > 0:
            k = L.estimate_key(C)
            if k.get("key") and (k.get("confidence") or 0) > 0.55:
                out["key"] = k["key"]
            pc = int(np.argmax(C.sum(axis=1)))
            out["rootNote"] = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][pc]
    # type from sound when the name does not say
    c = tb.get("centroidHz") or 0
    punchy = (tr.get("strengthDb") or 0) > 6
    if dur < 1.5:
        guess = ("kick" if c < 350 and punchy else "808" if c < 350 and tonal else "hat" if c > 5500
                 else "snare" if not tonal and punchy else "perc" if punchy else "oneshot")
    elif dur >= 2.0 and n_on >= 4:
        guess = "loop"
    elif tonal:
        guess = "pad" if dur > 3 else "tonal"
    else:
        guess = "fx"
    out["soundType"] = guess
    out["features"] = feat
    out["autoTags"] = tags + (["oneshot"] if dur < 1.5 else []) + (["tonal"] if tonal and "tonal" not in tags else [])
    return out


# ------------------------------------------------------------------ scan
def _entry(path: Path, tag: bool) -> dict:
    ext = path.suffix.lower()
    w = _words(path)
    e = {"id": _id(path), "path": str(path.resolve()), "name": path.stem, "ext": ext,
         "folder": path.parent.name, "size": path.stat().st_size, "mtime": int(path.stat().st_mtime)}
    types = _type_from_words(w)
    if ext in AUDIO:
        e["kind"] = "sample"
        if tag:
            e.update(tag_audio(path))
        if e.get("soundType") and e["soundType"] not in types:
            types.append(e["soundType"])
        e["bpm"] = _bpm_from_words(w) or e.get("bpm")
        e["key"] = _key_from_words(w) or e.get("key")
    else:
        e["kind"] = "instrument" if ext == ".sfz" else "preset"
        info = (read_fxp(path) if ext in (".fxp", ".fxb") else read_vstpreset(path) if ext == ".vstpreset"
                else read_sfz(path) if ext == ".sfz" else {"engine": {".vital": "vital", ".h2p": "u-he",
                                                                         ".nmsv": "massive"}.get(ext, "unknown"),
                                                             "format": ext[1:]})
        e.update(info)
        if info.get("category"):
            types += [t for t in _type_from_words(info["category"].lower().replace("/", " ")) if t not in types]
        e["name"] = info.get("name") or path.stem
    e["types"] = types
    e["tags"] = sorted(set(types + (e.get("autoTags") or []) + ([e["engine"]] if e.get("engine") else [])))
    e["playable"] = e["kind"] == "sample" or e.get("engine") in ("surge", "vst3", "sfz")
    return {k: v for k, v in e.items() if v is not None}


def scan(paths, tag: bool = True, log=None, limit: int = 20000) -> dict:
    idx = load()
    roots = [Path(p).expanduser() for p in (paths or [])]
    if not roots:
        from . import kit
        roots = [r for r in (kit.kit_dir() / "surge" / "patches", kit.kit_dir() / "sfz") if r.exists()]
        if not roots:
            raise ValueError("lib scan <folder> [<folder>...] (or install the kit first)")
    t0 = time.time()
    added = updated = skipped = errors = 0
    seen = 0
    for root in roots:
        if not root.exists():
            raise FileNotFoundError(f"no such folder: {root}")
        if str(root.resolve()) not in idx["roots"]:
            idx["roots"].append(str(root.resolve()))
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for p in files:
            ext = p.suffix.lower()
            if ext not in AUDIO and ext not in PRESETS:
                continue
            seen += 1
            if seen > limit:
                break
            iid = _id(p)
            old = idx["items"].get(iid)
            st = p.stat()
            if old and old.get("mtime") == int(st.st_mtime) and old.get("size") == st.st_size:
                skipped += 1
                continue
            try:
                idx["items"][iid] = _entry(p, tag)
                added += old is None
                updated += old is not None
            except Exception as e:  # one bad file never stops a scan
                errors += 1
                if log:
                    log(f"  skipped {p}: {e}")
    gone = [k for k, v in idx["items"].items() if not Path(v["path"]).exists()]
    for k in gone:
        del idx["items"][k]
    save(idx)
    kinds = {}
    for v in idx["items"].values():
        kinds[v["kind"]] = kinds.get(v["kind"], 0) + 1
    return {"index": str(index_path()), "added": added, "updated": updated, "unchanged": skipped,
            "removedMissing": len(gone), "errors": errors, "total": len(idx["items"]), "byKind": kinds,
            "seconds": round(time.time() - t0, 2)}


# ------------------------------------------------------------------ search
def _norm_tokens(q: str) -> list[str]:
    toks = [t for t in re.split(r"[\s,]+", q.lower()) if t]
    return [SYNONYMS.get(t, t) for t in toks]


def search(query: str = "", kind=None, engine=None, key=None, bpm=None, limit: int = 20) -> list[dict]:
    idx = load()
    toks = _norm_tokens(query or "")
    want_key = None
    if key:
        from .theory import parse_key, key_name
        t, s = parse_key(key)
        rel = ((t + 3) % 12, "major") if s == "minor" else ((t + 9) % 12, "minor")
        want_key = {key_name(t, s), key_name(*rel)}
    out = []
    for e in idx["items"].values():
        if kind and e.get("kind") != kind:
            continue
        if engine and e.get("engine") != engine:
            continue
        hay = " ".join([e.get("name", ""), e.get("folder", ""), e.get("category", ""), " ".join(e.get("tags", [])),
                        _words(Path(e["path"]))]).lower()
        hay_toks = set(re.split(r"[\s/]+", hay))
        sc = 0.0
        for t in toks:
            if t in e.get("types", []):
                sc += 3
            elif t in e.get("tags", []):
                sc += 2
            elif t in hay_toks:
                sc += 1.5
            elif t in hay:
                sc += 0.75
            else:
                sc -= 2
        if toks and sc <= 0:
            continue
        if want_key is not None:
            from .theory import parse_key, key_name
            try:
                ek = key_name(*parse_key(e["key"])) if e.get("key") else None
            except Exception:
                ek = None
            if ek not in want_key:
                continue
            sc += 1
        if bpm:
            eb = e.get("bpm")
            if not eb or min(abs(eb - bpm), abs(eb * 2 - bpm), abs(eb / 2 - bpm)) > 3:
                continue
            sc += 1
        out.append((sc, e["name"].lower(), e))
    out.sort(key=lambda x: (-x[0], x[1]))
    keys = ("id", "kind", "name", "engine", "types", "tags", "key", "bpm", "durationSec", "category", "path", "playable")
    return [{k: e[k] for k in keys if k in e} | {"score": round(sc, 2)} for sc, _, e in out[:int(limit)]]


def get(item_id: str) -> dict:
    idx = load()
    if item_id in idx["items"]:
        return idx["items"][item_id]
    hits = [v for v in idx["items"].values() if v["name"].lower() == str(item_id).lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise KeyError(f"{len(hits)} items are named {item_id!r}; use the id")
    raise KeyError(f"no library item {item_id!r} (try lib search)")


# ------------------------------------------------------------------ audition / use
PHRASES = {  # (t, len, key, vel) in quarter notes at 120 BPM, 2 bars
    "bass": [(i * 0.5, 0.45, 36 + (7 if i in (5, 13) else 0), 110) for i in range(16)],
    "chords": [(0, 3.8, k, 95) for k in (48, 55, 60, 63)] + [(4, 3.8, k, 95) for k in (44, 51, 56, 60)],
    "lead": [(t, 0.45, k, 105) for t, k in zip([0, 0.5, 1, 1.5, 2, 3, 4, 4.5, 5, 6], [72, 75, 79, 77, 75, 72, 70, 72, 75, 72])],
    "drums": [(i * 1.0, 0.2, 36, 120) for i in range(8)] + [(i + 0.5, 0.1, 42, 90) for i in range(8)]
             + [(1.0, 0.2, 38, 110), (3.0, 0.2, 38, 110), (5.0, 0.2, 38, 110), (7.0, 0.2, 38, 110)],
}


def _phrase_for(e) -> list:
    t = set(e.get("types", [])) | set(e.get("tags", []))
    if t & {"bass", "808"}:
        return PHRASES["bass"]
    if t & {"pad", "chords", "strings", "keys"}:
        return PHRASES["chords"]
    if t & {"kick", "snare", "hat", "perc", "drums"}:
        return PHRASES["drums"]
    return PHRASES["lead"]


def _vst3_for_class(class_id: str) -> str | None:
    idx = load()
    if class_id in idx.get("vst3Classes", {}):
        return idx["vst3Classes"][class_id]
    from .plugins.scan import scan as pscan
    import subprocess
    import sys
    for d in pscan(probe=False):
        if d["format"] != "vst3" or d["path"] in idx.get("vst3Probed", []):
            continue
        code = ("import pedalboard,sys;p=pedalboard.load_plugin(sys.argv[1]);"
                "sys.stdout.write(p.preset_data[8:40].decode('latin-1'))")
        try:
            r = subprocess.run([sys.executable, "-c", code, d["path"]], capture_output=True, timeout=60, text=True)
            cid = r.stdout.strip()
        except Exception:
            cid = ""
        idx.setdefault("vst3Probed", []).append(d["path"])
        if cid:
            idx.setdefault("vst3Classes", {})[cid] = d["path"]
        save(idx)
        if cid == class_id:
            return d["path"]
    return idx.get("vst3Classes", {}).get(class_id)


def _instrument_for(e, states_dir: Path) -> dict:
    """Instrument spec for a playable preset/instrument item; state files go into states_dir."""
    from . import kit
    if e.get("engine") == "surge":
        if not kit.installed()["surge"] and not kit.resolve_ref(f"clap:{kit.SURGE_ID}"):
            from .plugins.scan import resolve
            try:
                resolve(f"clap:{kit.SURGE_ID}")
            except FileNotFoundError:
                raise ValueError("Surge XT is needed to play this preset: run `plaindaw kit install`")
        data = Path(e["path"]).read_bytes()
        st = states_dir / f"lib_{e['id']}.bin"
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_bytes(data[kit.SURGE_FXP_HEADER:])
        return {"plugin": f"clap:{kit.SURGE_ID}", "state": st.name}
    if e.get("engine") == "vst3":
        path = _vst3_for_class(e.get("classId", ""))
        if not path:
            raise ValueError(f"no installed VST3 plugin matches this preset (class {e.get('classId')})")
        st = states_dir / f"lib_{e['id']}.vstpreset"
        st.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(e["path"], st)
        return {"plugin": f"vst3:{Path(path).stem}", "path": path, "state": st.name}
    if e.get("engine") == "sfz":
        return {"plugin": f"sfz:{e['path']}"}
    raise ValueError(f"{e.get('engine')} presets are indexed but cannot be played headless here")


def audition(item_id: str, out: str | None = None, note: str | None = None, seconds: float | None = None) -> dict:
    import soundfile as sf
    from .listen import listen as L
    from .model import new_project
    from .render import render
    e = get(item_id)
    dest = Path(out) if out else index_path().parent / "auditions" / f"{e['id']}.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if e["kind"] == "sample":
        from .media import decode
        x = np.asarray(decode(Path(e["path"]), 48000))[:, : int((seconds or 10) * 48000)]
        sr, warnings = 48000, []
    else:
        with tempfile.TemporaryDirectory(prefix="pd-audition-") as td:
            doc = new_project("audition", 120)
            spec = _instrument_for(e, Path(td) / "states")
            if spec.get("state"):
                spec["state"] = f"states/{spec['state']}"
            notes = _phrase_for(e)
            if note:
                from .timebase import note_number
                shift = note_number(note) - 60
                notes = [(t, ln, k + shift, v) for t, ln, k, v in notes]
            doc["patterns"]["p"] = {"lengthBeats": 8, "notes": [{"t": t, "len": ln, "key": k, "vel": v} for t, ln, k, v in notes]}
            master = doc["tracks"][-1]
            doc["tracks"].insert(0, {"id": "a", "type": "instrument", "name": e["name"], "instrument": spec,
                                     "clips": [{"id": "c", "pattern": "p", "at": "1.1.1"}], "inserts": [],
                                     "mix": {"gainDb": 0.0, "pan": 0.0}, "sends": [], "output": master["id"]})
            doc["settings"]["tailSec"] = 1.5
            res = render(doc, Path(td), end="3.1.1")
            x, sr, warnings = res.mix, res.sr, list(res.warnings)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > 0.99:
        x = x * (0.99 / peak)
    sf.write(str(dest), x.T.astype(np.float32), sr, subtype="PCM_24")
    rep = L(x, sr)
    return {"id": e["id"], "name": e["name"], "kind": e["kind"], "path": str(dest), "seconds": round(x.shape[1] / sr, 2),
            "renderSec": round(time.time() - t0, 2), "heard": rep.get("summary", [])[:4],
            "tags": rep["timbre"]["mix"].get("tags"), **({"warnings": warnings} if warnings else {})}


def use_ops(s, item_id: str, track: str | None = None, at: str = "1.1.1", as_: str | None = None) -> tuple[list, str]:
    """Ops that bring a library item into the project (new track)."""
    from .model import all_ids, unique_id
    e = get(item_id)
    ids = set(all_ids(s.doc))
    name = track or e["name"]
    tid = unique_id(ids, re.sub(r"[^A-Za-z0-9_.-]", "_", name.lower())[:40] or "lib")
    if e["kind"] == "sample":
        if (as_ or "audio") == "sampler":
            ops = [{"op": "media.import", "args": {"path": e["path"]}}]
            from .media import import_file
            rel = import_file(s.dir, Path(e["path"]))
            ops = [{"op": "track.add", "args": {"name": name, "id": tid, "plugin": "builtin:sampler",
                                                "params": {"file": rel, "root": 60, "mode": "oneshot"}}}]
        else:
            ops = [{"op": "track.add", "args": {"name": name, "id": tid, "type": "audio"}},
                   {"op": "clip.add", "args": {"track": tid, "file": e["path"], "at": at}}]
        how = "sample"
    else:
        spec = _instrument_for(e, s.dir / "states")
        if spec.get("state"):
            spec["state"] = f"states/{spec['state']}"
        ops = [{"op": "track.add", "args": {"name": name, "id": tid, **spec}}]
        how = f"{e.get('engine')} preset"
    return ops, f"lib use: {e['name']} ({how}) on new track {name}"
