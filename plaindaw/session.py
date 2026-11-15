"""A Session owns one project folder: loading, the op layer, undo history, locks, render/analysis.

Every front end (CLI, JSON-RPC, MCP, GUI) talks to a Session through `call(method, params)`.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import re
import os
import threading
import time
from pathlib import Path

import numpy as np

from . import FORMAT, __version__
from . import patch as P
from .model import ModelError, dumps, load, new_project, normalize, track as find_track, validate
from .ops import REGISTRY, Ctx, OpError, apply_op, op_catalog
from .timebase import TimeMap, note_name

PROJECT_FILE = "project.json"


class PermissionDenied(OpError):
    pass


class NeedsConfirm(OpError):
    pass


def _now():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _jsonable(v):
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def create_project(path: Path, title: str = "Untitled", bpm: float = 120, num: int = 4, den: int = 4,
                   sample_rate: int = 48000, author: str = "") -> Path:
    path = Path(path)
    if (path / PROJECT_FILE).exists():
        raise OpError(f"{path} already contains a project")
    path.mkdir(parents=True, exist_ok=True)
    for d in ("media", "states", "history", "renders"):
        (path / d).mkdir(exist_ok=True)
    doc = new_project(title, bpm, num, den, sample_rate, author)
    (path / PROJECT_FILE).write_text(dumps(doc), encoding="utf-8")
    (path / ".gitignore").write_text("renders/\n", encoding="utf-8")
    return path


class Session:
    def __init__(self, project_dir: Path, author: str = "human", mode: str = "allow"):
        self.dir = Path(project_dir).resolve()
        f = self.dir / PROJECT_FILE
        if not f.exists():
            raise OpError(f"no {PROJECT_FILE} in {self.dir} (create one with: plaindaw new {self.dir})")
        self.author = author
        if mode not in ("allow", "read-only", "ask"):
            raise ValueError("mode must be allow, read-only or ask")
        self.mode = mode
        self.lock = threading.RLock()
        self.listeners = []
        self.version = 0
        self._load()

    # ---------------------------------------------------------------- persistence
    def _load(self):
        raw = load(self.dir / PROJECT_FILE)
        if not str(raw.get("format", "")).startswith("plaindaw/"):
            raise OpError(f"not a PlainDAW project (format={raw.get('format')!r})")
        self.doc = normalize(raw)
        errs = validate(self.doc)
        if errs:
            raise OpError("project is invalid: " + "; ".join(errs[:8]))
        self._mtime = (self.dir / PROJECT_FILE).stat().st_mtime_ns
        self.entries = []        # history entries (op events)
        self.applied = []        # stack of entry n currently applied (undoable)
        self.redo_stack = []
        hf = self.dir / "history" / "ops.jsonl"
        if hf.exists():
            for line in hf.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                self.entries.append(e)
                self._track_stack(e)

    def _track_stack(self, e):
        k = e.get("kind", "op")
        if k in ("op", "revert"):
            self.applied.append(e["n"]); self.redo_stack.clear()
        elif k == "undo":
            if self.applied and self.applied[-1] == e["of"]:
                self.applied.pop()
            self.redo_stack.append(e["of"])
        elif k == "redo":
            if self.redo_stack and self.redo_stack[-1] == e["of"]:
                self.redo_stack.pop()
            self.applied.append(e["of"])
        elif k == "external":
            self.applied.clear(); self.redo_stack.clear()

    def reload_if_changed(self) -> bool:
        """Pick up edits made by another process or by hand in a text editor."""
        with self.lock:
            m = (self.dir / PROJECT_FILE).stat().st_mtime_ns
            if m == self._mtime:
                return False
            old = self.doc
            self._load()
            hist_tail = self.entries[-1] if self.entries else None
            if hist_tail is None or hist_tail.get("time_ns", 0) < m - 1_000_000:
                # edited outside PlainDAW: record it so it shows in history
                fwd = P.diff(old, self.doc)
                if fwd:
                    self._append({"kind": "external", "author": "external", "op": "file.edit", "args": {},
                                  "forward": fwd, "inverse": P.invert(old, fwd), "summary": "edited outside PlainDAW"})
            self._notify({"type": "reload"})
            return True

    def _save(self):
        tmp = self.dir / (PROJECT_FILE + ".tmp")
        tmp.write_text(dumps(self.doc), encoding="utf-8")
        os.replace(tmp, self.dir / PROJECT_FILE)
        self._mtime = (self.dir / PROJECT_FILE).stat().st_mtime_ns

    def _append(self, e):
        e["n"] = (self.entries[-1]["n"] + 1) if self.entries else 1
        e["time"] = _now()
        e["time_ns"] = time.time_ns()
        (self.dir / "history").mkdir(exist_ok=True)
        with open(self.dir / "history" / "ops.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(_jsonable(e), ensure_ascii=False, separators=(",", ":")) + "\n")
        self.entries.append(e)
        self._track_stack(e)
        return e

    def _notify(self, ev):
        self.version += 1
        ev["version"] = self.version
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                pass

    # ---------------------------------------------------------------- locks / permissions
    def _touched_owners(self, before, after, fwd):
        owners = set()
        for st in fwd:
            p = st["path"]
            if len(p) >= 2 and p[0] == "tracks" and isinstance(p[1], int):
                for d in (before, after):
                    if p[1] < len(d["tracks"]):
                        owners.add("track:" + d["tracks"][p[1]]["id"])
                if len(p) == 2 and st["op"] == "add":
                    owners.add("track:" + st["value"]["id"])
            elif len(p) == 1 and p[0] == "tracks":
                owners.update("track:" + t["id"] for t in before["tracks"])
            elif len(p) >= 2 and p[0] == "patterns":
                owners.add("pattern:" + str(p[1]))
            elif p and p[0] == "locks":
                owners.add("locks")
        return owners

    def _check_locks(self, before, after, fwd, author, opname):
        owners = self._touched_owners(before, after, fwd)
        for l in before.get("locks", []):
            if l["target"] in owners and (l["against"] == "all" or author == "ai"):
                if opname == "lock.clear" and author != "ai":
                    continue
                raise PermissionDenied(f"{l['target']} is locked (against {l['against']}, set by {l.get('by', '?')}); "
                                       f"{opname} refused")
        if "locks" in owners and author == "ai":
            for l in before.get("locks", []):
                if l.get("by") != "ai" and l["target"] not in {x["target"] for x in after.get("locks", [])}:
                    raise PermissionDenied("the AI cannot remove a lock a person set")

    # ---------------------------------------------------------------- ops
    def apply(self, name: str, args: dict | None = None, author: str | None = None, dry_run: bool = False,
              confirm: bool = False, reason: str | None = None, label: str | None = None) -> dict:
        author = author or self.author
        args = dict(args or {})
        reason = args.pop("reason", None) or reason
        dry_run = bool(args.pop("dryRun", dry_run))
        confirm = bool(args.pop("confirm", confirm))
        with self.lock:
            self.reload_if_changed()
            if self.mode == "read-only" and author == "ai":
                raise PermissionDenied("session is read-only for the AI")
            spec = REGISTRY.get(name)
            if spec is None:
                raise OpError(f"unknown op {name!r}. See ops.list")
            destructive = spec.destructive or (name == "batch" and any(
                REGISTRY.get(i.get("op")) and REGISTRY[i["op"]].destructive for i in args.get("ops", [])))
            if self.mode == "ask" and author == "ai" and destructive and not confirm and not dry_run:
                raise NeedsConfirm(f"{name} is destructive; ask the person, then resend with confirm=true")
            before = self.doc
            after, res = apply_op(before, name, args, Ctx(self.dir, author))
            after = normalize(after)
            fwd = P.diff(before, after)
            self._check_locks(before, after, fwd, author, name)
            summary = describe_patch(before, after, fwd, limit=6)
            if dry_run:
                return {"ok": True, "dryRun": True, "op": name, "changes": summary, **res}
            if not fwd:
                return {"ok": True, "op": name, "changed": False, "changes": [], **res}
            inv = P.invert(before, fwd)
            self.doc = after
            self._save()
            entry = {"kind": "op", "author": author, "op": label or name, "args": args, "forward": fwd,
                     "inverse": inv, "summary": summary[:3]}
            if reason:
                entry["reason"] = str(reason)[:500]
            e = self._append(entry)
            self._notify({"type": "op", "n": e["n"], "op": label or name, "author": author})
            return {"ok": True, "n": e["n"], "op": label or name, "changes": summary, **res}

    def _entry(self, n):
        for e in self.entries:
            if e["n"] == n:
                return e
        raise OpError(f"no history entry #{n}")

    def undo(self, author=None) -> dict:
        author = author or self.author
        with self.lock:
            self.reload_if_changed()
            if self.mode == "read-only" and author == "ai":
                raise PermissionDenied("session is read-only for the AI")
            if not self.applied:
                raise OpError("nothing to undo")
            e = self._entry(self.applied[-1])
            new = P.apply(copy.deepcopy(self.doc), e["inverse"])
            self._check_locks(self.doc, new, e["inverse"], author, "undo")
            self.doc = new; self._save()
            u = self._append({"kind": "undo", "of": e["n"], "author": author, "op": "history.undo",
                              "summary": [f"undid #{e['n']} {e['op']}"]})
            self._notify({"type": "undo", "n": u["n"], "of": e["n"]})
            return {"ok": True, "undid": e["n"], "op": e["op"], "by": e["author"]}

    def redo(self, author=None) -> dict:
        author = author or self.author
        with self.lock:
            self.reload_if_changed()
            if not self.redo_stack:
                raise OpError("nothing to redo")
            e = self._entry(self.redo_stack[-1])
            new = P.apply(copy.deepcopy(self.doc), e["forward"])
            self._check_locks(self.doc, new, e["forward"], author, "redo")
            self.doc = new; self._save()
            r = self._append({"kind": "redo", "of": e["n"], "author": author, "op": "history.redo",
                              "summary": [f"redid #{e['n']} {e['op']}"]})
            self._notify({"type": "redo", "n": r["n"], "of": e["n"]})
            return {"ok": True, "redid": e["n"], "op": e["op"]}

    def revert(self, n: int, author=None) -> dict:
        """Undo one specific earlier op, if nothing later changed the same data."""
        author = author or self.author
        with self.lock:
            self.reload_if_changed()
            e = self._entry(int(n))
            if e.get("kind") not in ("op", "revert") or e["n"] not in self.applied:
                raise OpError(f"#{n} is not a currently applied edit")
            F, I = e["forward"], e["inverse"]
            cur = copy.deepcopy(self.doc)
            try:
                for k, inv in enumerate(I):
                    f = F[len(F) - 1 - k]
                    if f["op"] in ("replace", "add"):
                        if P.get(cur, f["path"], None) != f["value"]:
                            raise OpError("conflict")
                    cur = P.apply(cur, [inv])
            except (OpError, KeyError, IndexError, TypeError):
                raise OpError(f"cannot revert #{n}: later edits changed the same data (undo those first)")
            cur = normalize(cur)
            errs = validate(cur)
            if errs:
                raise OpError(f"cannot revert #{n}: result invalid ({errs[0]})")
            fwd = P.diff(self.doc, cur)
            self._check_locks(self.doc, cur, fwd, author, "revert")
            inv = P.invert(self.doc, fwd)
            before = self.doc
            self.doc = cur; self._save()
            r = self._append({"kind": "revert", "of": e["n"], "author": author, "op": "history.revert",
                              "args": {"n": e["n"]}, "forward": fwd, "inverse": inv,
                              "summary": [f"reverted #{e['n']} {e['op']}"] + describe_patch(before, cur, fwd, 3)})
            self._notify({"type": "revert", "n": r["n"], "of": e["n"]})
            return {"ok": True, "reverted": e["n"], "n": r["n"]}

    def history(self, limit: int = 30) -> list:
        undone = set(self.redo_stack)
        applied = set(self.applied)
        out = []
        for e in self.entries[-limit:]:
            st = ("applied" if e["n"] in applied else "undone" if e["n"] in undone else "") if e.get("kind") in ("op", "revert") else ""
            out.append({"n": e["n"], "time": e.get("time"), "author": e.get("author"), "op": e.get("op"),
                        "kind": e.get("kind", "op"), "state": st, "summary": e.get("summary", []),
                        **({"of": e["of"]} if "of" in e else {}), **({"reason": e["reason"]} if e.get("reason") else {})})
        return out

    # ---------------------------------------------------------------- queries
    def summary(self) -> dict:
        d = self.doc
        tm = TimeMap(d["transport"])
        from .render import song_end_qn
        end = song_end_qn(d, tm, self.dir)
        tracks = []
        for t in d["tracks"]:
            row = {"id": t["id"], "type": t["type"], "name": t["name"], "gainDb": t["mix"].get("gainDb", 0.0),
                   "pan": t["mix"].get("pan", 0.0)}
            for k in ("mute", "solo"):
                if t["mix"].get(k):
                    row[k] = True
            if t.get("instrument"):
                row["instrument"] = t["instrument"]["plugin"]
            if t.get("inserts"):
                row["inserts"] = [f"{p['id']}={p['plugin']}" + (" (bypassed)" if p.get("bypass") else "") for p in t["inserts"]]
            if t.get("sends"):
                row["sends"] = {s["to"]: s["levelDb"] for s in t["sends"]}
            if t["type"] != "master":
                row["output"] = t.get("output")
            if "clips" in t:
                row["clips"] = len(t["clips"])
            if t.get("automation"):
                row["automation"] = [a["target"] for a in t["automation"]]
            tracks.append(row)
        return {"title": d["meta"].get("title"), "format": d.get("format"),
                "tempo": [{"at": x["at"], "bpm": x["bpm"]} for x in d["transport"]["tempo"]],
                "timeSig": [f"{x['num']}/{x['den']} @ {x['at']}" for x in d["transport"].get("timeSig", [])],
                "key": d["transport"].get("keySig"), "lengthBars": tm.pos(end).split(".")[0] if end else "0",
                "lengthSec": round(tm.sec(end), 2),
                "markers": [f"{m['name']} @ {m['at']}" for m in d.get("markers", [])], "tracks": tracks,
                "patterns": {k: {"lengthBeats": v["lengthBeats"], "notes": len(v["notes"])} for k, v in d["patterns"].items()},
                "locks": d.get("locks", []), "historyHead": self.entries[-1]["n"] if self.entries else 0,
                "canUndo": bool(self.applied), "canRedo": bool(self.redo_stack)}

    def layout(self) -> dict:
        """Doc plus computed clip spans (quarter notes) for the GUI."""
        from .render import clip_len_qn, song_end_qn
        tm = TimeMap(self.doc["transport"])
        clips = {}
        for t in self.doc["tracks"]:
            for c in t.get("clips", []):
                clips[c["id"]] = {"start": tm.qn(c["at"]), "len": clip_len_qn(self.doc, tm, c, self.dir)}
        bars = []
        end = max(song_end_qn(self.doc, tm, self.dir), 16.0) + 8
        b = 1
        while True:
            q = tm.qn(f"{b}.1.1")
            if q > end:
                break
            bars.append(q); b += 1
        markers = [{"name": m["name"], "qn": tm.qn(m["at"])} for m in self.doc.get("markers", [])]
        return {"doc": self.doc, "clips": clips, "bars": bars, "markers": markers, "endQn": end,
                "history": self.history(60), "canUndo": bool(self.applied), "canRedo": bool(self.redo_stack),
                "version": self.version}

    def pattern_text(self, pid: str) -> dict:
        from .model import pattern
        p = pattern(self.doc, pid)
        return {"id": pid, "lengthBeats": p["lengthBeats"],
                "notes": [{**n, "name": note_name(n["key"])} for n in p["notes"]]}

    # ---------------------------------------------------------------- render / analyse / export
    def sections(self, start_sec=0.0, end_sec=None):
        tm = TimeMap(self.doc["transport"])
        ms = sorted(self.doc.get("markers", []), key=lambda m: tm.qn(m["at"]))
        out = []
        for i, m in enumerate(ms):
            s = tm.to_sec(m["at"]) - start_sec
            e = (tm.to_sec(ms[i + 1]["at"]) - start_sec) if i + 1 < len(ms) else end_sec
            if e is None or e <= 0:
                continue
            out.append((m["name"], max(0.0, s), e))
        return out

    def render(self, start=None, end=None, stems=False, sr=None, sandbox=None):
        from .render import render
        with self.lock:
            self.reload_if_changed()
            doc = copy.deepcopy(self.doc)
        return render(doc, self.dir, sr=sr, start=start, end=end, stems=stems, sandbox=sandbox)

    def analyze(self, res=None, target=None, start=None, end=None) -> dict:
        from .analysis import analyze
        res = res or self.render(start, end, stems=True)
        names = {t["id"]: t["name"] for t in self.doc["tracks"]}
        rep = analyze(res.mix, res.sr, stems={k: v for k, v in res.stems.items() if k != "master"},
                      sections=self.sections(res.start_sec, res.duration), target=target, names=names,
                      no_masking={t["id"] for t in self.doc["tracks"] if t["type"] in ("return", "bus")})
        if res.warnings:
            rep["renderWarnings"] = list(res.warnings)
        return rep

    def listen(self, res=None, start=None, end=None, reference=None, tracks=None) -> dict:
        """Musical listening report (key, chords, tempo, timing, transients, timbre, energy)."""
        from .listen import compare_reference, listen, report_text
        res = res or self.render(start, end, stems=True)
        tm = TimeMap(self.doc["transport"])
        q0 = tm.qn(start) if start is not None else 0.0
        bpm = tm.bpm_at(q0)
        sig = tm._sig_for_qn(q0)
        bar_sec = sig.qn_per_bar * 60.0 / bpm
        names = {t["id"]: t["name"] for t in self.doc["tracks"]}
        stems = {k: v for k, v in res.stems.items() if k != "master" and (not tracks or k in tracks)}
        # first beat: the next bar line at or after the render start
        fb = 0.0
        rep = listen(res.mix, res.sr, bpm_hint=bpm, bar_sec=bar_sec, stems=stems, names=names,
                     sections=self.sections(res.start_sec, res.duration), first_beat_sec=fb)
        rep["projectTempo"] = bpm
        if self.doc["transport"].get("keySig"):
            rep["projectKey"] = self.doc["transport"]["keySig"]
        if reference:
            import soundfile as sf
            from .media import decode
            rp = Path(reference)
            if not rp.is_absolute() and (self.dir / rp).exists():
                rp = self.dir / rp
            ref = decode(rp, res.sr)
            rep["reference"] = compare_reference(res.mix, np.array(ref), res.sr, res.sr, bpm_hint=bpm)
            rep["reference"]["file"] = str(rp)
        rep["text"] = report_text(rep)
        return rep

    def export(self, out: str | None = None, formats=("wav",), bits="24", bitrate="320k", start=None, end=None,
               stems=False, analyze=True, target=None, name=None) -> dict:
        from .export import write_audio
        res = self.render(start, end, stems=True)
        base = name or (self.doc["meta"].get("title") or "mix")
        base = "".join(c if c.isalnum() or c in "-_ " else "_" for c in base).strip().replace(" ", "_") or "mix"
        outdir = Path(out) if out else self.dir / "renders"
        if not outdir.is_absolute():
            outdir = self.dir / outdir
        files = []
        tags = {"title": self.doc["meta"].get("title"), "artist": self.doc["meta"].get("author")}
        if isinstance(formats, str):
            formats = [f.strip() for f in formats.split(",") if f.strip()]
        for fmt in formats:
            files.append(write_audio(outdir / f"{base}.{fmt}", res.mix, res.sr, bits=bits, bitrate=bitrate, tags=tags))
        if stems:
            for tid, x in res.stems.items():
                if tid == "master":
                    continue
                files.append(write_audio(outdir / "stems" / f"{base}_{tid}.wav", x, res.sr, bits=bits))
        rep = {"ok": True, "files": files, "seconds": round(res.duration, 3), "warnings": res.warnings}
        if analyze:
            a = self.analyze(res, target=target)
            rep["analysis"] = a
            (outdir / f"{base}.analysis.json").write_text(json.dumps(a, indent=2), encoding="utf-8")
        self._notify({"type": "render", "files": [f["path"] for f in files]})
        return rep

    # ---------------------------------------------------------------- import
    def import_midi(self, path, track_prefix="", plugin="builtin:synth", at="1.1.1", tempo=True) -> dict:
        from .midi import read_midi
        m = read_midi(Path(path))
        ops = []
        if tempo and m["tempo"]:
            ops.append({"op": "tempo.set", "args": {"bpm": m["tempo"][0][1]}})
        from .model import unique_id, all_ids
        ids = set(all_ids(self.doc))
        made = []
        for i, t in enumerate(m["tracks"]):
            name = (track_prefix + t["name"]).strip() or f"MIDI {i + 1}"
            tid = unique_id(ids, name); ids.add(tid)
            pid = unique_id(set(self.doc["patterns"]) | ids, tid + "_midi"); ids.add(pid)
            inst = "builtin:drums" if t["channel"] == 9 else plugin
            L = max(4.0, -(-max(n["t"] + n["len"] for n in t["notes"]) // 4) * 4)
            ops += [{"op": "track.add", "args": {"name": name, "id": tid, "type": "instrument", "plugin": inst}},
                    {"op": "pattern.add", "args": {"id": pid, "lengthBeats": L, "notes": t["notes"]}},
                    {"op": "clip.add_pattern", "args": {"track": tid, "pattern": pid, "at": at}}]
            made.append({"track": tid, "pattern": pid, "notes": len(t["notes"])})
        r = self.apply("batch", {"ops": ops})
        return {"ok": True, "n": r.get("n"), "tracks": made}

    def import_stems(self, paths, at="1.1.1") -> dict:
        ops = []
        from .model import unique_id, all_ids
        ids = set(all_ids(self.doc))
        made = []
        for p in paths:
            name = Path(p).stem
            tid = unique_id(ids, name); ids.add(tid)
            ops += [{"op": "track.add", "args": {"name": name, "id": tid, "type": "audio"}},
                    {"op": "clip.add", "args": {"track": tid, "file": str(Path(p).resolve()), "at": at}}]
            made.append(tid)
        r = self.apply("batch", {"ops": ops})
        return {"ok": True, "n": r.get("n"), "tracks": made}

    # ---------------------------------------------------------------- dispatcher
    def call(self, method: str, params: dict | None = None, author: str | None = None):
        params = dict(params or {})
        if method in REGISTRY:
            return self.apply(method, params, author=author)
        fn = QUERIES.get(method)
        if fn is None:
            raise OpError(f"unknown method {method!r}. See ops.list")
        return fn(self, params, author)


# ------------------------------------------------------------------ musical diff
def _track_label(doc, i):
    try:
        t = doc["tracks"][i]
        return f"{t['name']} ({t['id']})"
    except (IndexError, KeyError, TypeError):
        return f"track {i}"


def describe_patch(before, after, fwd, limit=20) -> list[str]:
    lines = []
    for st in fwd:
        p = st["path"]
        if p and p[0] == "tracks" and len(p) >= 2:
            ref = before if st["op"] == "remove" else after
            label = _track_label(ref, p[1]) if isinstance(p[1], int) else "tracks"
            rest = "/".join(str(x) for x in p[2:])
            if len(p) == 2:
                lines.append(f"{'added' if st['op'] == 'add' else 'removed' if st['op'] == 'remove' else 'changed'} track {label}")
                continue
            if len(p) == 4 and p[2] == "clips" and st["op"] in ("add", "remove"):
                c = st.get("value") if st["op"] == "add" else P.get(before, p, {})
                what = (c or {}).get("pattern") or (c or {}).get("file") or ""
                lines.append(f"{label}: {'added' if st['op'] == 'add' else 'removed'} clip {(c or {}).get('id', '')} {what} @ {(c or {}).get('at', '')}".rstrip())
                continue
        elif p and p[0] == "patterns" and len(p) >= 2:
            label, rest = f"pattern {p[1]}", "/".join(str(x) for x in p[2:])
            if len(p) == 2:
                lines.append(f"{'added' if st['op'] == 'add' else 'removed' if st['op'] == 'remove' else 'changed'} {label}")
                continue
            if len(p) == 4 and p[2] == "notes":
                if st["op"] in ("add", "remove"):
                    n = st.get("value") if st["op"] == "add" else P.get(before, p, None)
                    if isinstance(n, dict):
                        lines.append(f"{label}: {'+' if st['op'] == 'add' else '-'} {note_name(n['key'])} t={n['t']} len={n['len']} vel={n.get('vel')}")
                        continue
        else:
            label, rest = "project", "/".join(str(x) for x in p)
        if st["op"] == "replace":
            old = P.get(before, p, None)
            v = st["value"]
            s_old = json.dumps(old) if not isinstance(old, (dict, list)) else "…"
            s_new = json.dumps(v) if not isinstance(v, (dict, list)) else "…"
            lines.append(f"{label}: {rest} {s_old} → {s_new}")
        elif st["op"] == "add" and isinstance(st["value"], dict) and "plugin" in st["value"]:
            v = st["value"]
            lines.append(f"{label}: added {v.get('id', '')} = {v['plugin']}" + (f" (sidechain {v['sidechain']})" if v.get("sidechain") else ""))
        elif st["op"] == "add":
            v = st["value"]
            lines.append(f"{label}: + {rest} = {json.dumps(v) if not isinstance(v, (dict, list)) or len(json.dumps(v)) < 80 else '…'}")
        else:
            lines.append(f"{label}: - {rest}")
    if len(lines) > limit:
        lines = lines[:limit] + [f"… and {len(lines) - limit} more changes"]
    return lines


def diff_docs(a, b) -> list[str]:
    return describe_patch(a, b, P.diff(a, b), limit=200)


# ------------------------------------------------------------------ query methods
def _q_render_or_export(s, p, author):
    return s.export(out=p.get("out"), formats=p.get("formats", p.get("format", "wav")), bits=str(p.get("bits", "24")),
                    bitrate=p.get("bitrate", "320k"), start=p.get("start"), end=p.get("end"),
                    stems=bool(p.get("stems")), analyze=p.get("analyze", True), target=p.get("target"),
                    name=p.get("name"))


def _q_analyze(s, p, author):
    res = s.render(p.get("start"), p.get("end"), stems=True)
    rep = s.analyze(res, target=p.get("target"))
    if p.get("reference") or p.get("listen"):
        rep["listen"] = s.listen(res, p.get("start"), p.get("end"), reference=p.get("reference"))
    return rep


def _q_listen(s, p, author):
    tr = p.get("tracks")
    if isinstance(tr, str):
        tr = [x for x in tr.replace(",", " ").split() if x]
    start, end = p.get("start"), p.get("end")
    if p.get("bars") is not None:
        from .musical import bar_range
        start, end = bar_range(s.doc, p["bars"])
    return s.listen(start=start, end=end, reference=p.get("reference"), tracks=tr)


def _q_plugin_scan(s, p, author):
    from . import dsp
    return {"builtin": {"instruments": [f"builtin:{x}" for x in dsp.INSTRUMENTS],
                        "effects": [f"builtin:{x}" for x in dsp.FX]},
            "external": _scan(p)}


def _scan(p):
    from .plugins.scan import default_dirs, reset_cache, scan
    dirs = default_dirs()
    if p.get("path"):
        dirs = [Path(x) for x in str(p["path"]).split(os.pathsep)] + dirs
    reset_cache()
    return scan(dirs)


def _q_plugin_params(s, p, author):
    ref = p.get("plugin")
    if not ref and p.get("track"):
        t = find_track(s.doc, p["track"])
        if p.get("insert"):
            from .model import insert
            ref = insert(t, p["insert"])["plugin"]
            path = insert(t, p["insert"]).get("path")
        else:
            ref = t["instrument"]["plugin"]; path = t["instrument"].get("path")
    else:
        path = p.get("path")
    if not ref:
        raise OpError("give plugin=<ref> or track (+insert)")
    fmt, _, name = ref.partition(":")
    if fmt == "builtin":
        from .dsp import PARAMS
        if name not in PARAMS:
            raise OpError(f"unknown built-in {name!r}")
        d = PARAMS[name]
        return {"plugin": ref, "params": {k: v for k, v in d.items() if not k.startswith("_")},
                "automatable": d.get("_auto", []), "doc": d.get("_doc", "")}
    from .plugins.sandbox import run_job
    return run_job({"kind": "describe", "ref": ref, "path": path}, sandbox=True)


def _q_diff(s, p, author):
    if p.get("file"):
        other = normalize(load(Path(p["file"]) / PROJECT_FILE if Path(p["file"]).is_dir() else Path(p["file"])))
        return {"changes": diff_docs(other, s.doc)}
    n = int(p.get("since", 0))
    # rebuild the doc as it was after entry n by undoing later applied entries' patches
    doc = copy.deepcopy(s.doc)
    for e in reversed(s.entries):
        if e["n"] <= n:
            break
        k = e.get("kind", "op")
        if k in ("op", "revert", "external"):
            if e["n"] in s.applied or k == "external":
                doc = P.apply(doc, e["inverse"])
    return {"since": n, "changes": diff_docs(doc, s.doc)}


QUERIES = {
    "project.get": lambda s, p, a: s.doc,
    "project.summary": lambda s, p, a: s.summary(),
    "project.layout": lambda s, p, a: s.layout(),
    "project.validate": lambda s, p, a: {"errors": validate(s.doc)},
    "track.list": lambda s, p, a: s.summary()["tracks"],
    "pattern.get": lambda s, p, a: s.pattern_text(p.get("pattern") or p.get("id")),
    "history.list": lambda s, p, a: s.history(int(p.get("limit", 30))),
    "history.undo": lambda s, p, a: s.undo(a),
    "history.redo": lambda s, p, a: s.redo(a),
    "history.revert": lambda s, p, a: s.revert(p["n"], a),
    "ops.list": lambda s, p, a: op_catalog(),
    "plugin.scan": _q_plugin_scan,
    "plugin.params": _q_plugin_params,
    "render": _q_render_or_export,
    "export.audio": _q_render_or_export,
    "analyze": _q_analyze,
    "listen": _q_listen,
    "export.midi": lambda s, p, a: __import__("plaindaw.midi", fromlist=["x"]).export_midi(
        s.doc, Path(p.get("out") or s.dir / "renders" / "song.mid"), p.get("tracks")),
    "import.midi": lambda s, p, a: s.import_midi(p["path"], p.get("prefix", ""), p.get("plugin", "builtin:synth"),
                                                 p.get("at", "1.1.1"), p.get("tempo", True)),
    "import.stems": lambda s, p, a: s.import_stems(p["paths"] if isinstance(p["paths"], list) else [p["paths"]],
                                                   p.get("at", "1.1.1")),
    "diff": _q_diff,
    "media.gc": lambda s, p, a: _media_gc(s, p),
}


def _media_gc(s, p):
    from .media import used_media
    used = used_media(s.doc)
    for e in s.entries:  # keep media referenced anywhere in history so undo still works
        used |= {x for x in _strings(e.get("inverse", [])) if x.startswith("media/")}
    gone = []
    for f in (s.dir / "media").glob("*"):
        rel = f"media/{f.name}"
        if f.is_file() and rel not in used and not f.name.startswith("."):
            gone.append(rel)
            if not p.get("dryRun"):
                f.unlink()
    return {"removed" if not p.get("dryRun") else "wouldRemove": gone}


def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


METHODS_DOC = {
    "project.get": "Full project JSON", "project.summary": "Compact overview (tracks, tempo, markers, patterns)",
    "project.validate": "List problems", "track.list": "Tracks with mix/plugins", "pattern.get": "Notes of a pattern {pattern}",
    "history.list": "Recent edits {limit}", "history.undo": "Undo last edit", "history.redo": "Redo",
    "history.revert": "Undo one specific earlier edit {n}", "ops.list": "All edit ops with their arguments",
    "plugin.scan": "Find installed VST3/CLAP plugins {path?, refresh?}", "plugin.params": "Parameters of a plugin {plugin|track,insert}",
    "render": "Render + write audio {formats: 'wav,mp3', bits, bitrate, start, end, stems, target, out, name}",
    "export.audio": "Same as render", "analyze": "Render and measure {target: club|streaming|podcast|film, start, end}",
    "export.midi": "Write a .mid {out, tracks?}", "import.midi": "Import a .mid as instrument tracks {path, plugin?}",
    "import.stems": "Import audio files as tracks {paths}", "diff": "Musical diff {since: n | file}",
    "media.gc": "Delete unused media {dryRun}",
    "listen": "Musical listening report {start, end, reference: file.wav, tracks}",
}

# ---- musical commands (compose, arrange, fix clash, variations, humanize, preview, instrument add)
from . import musical as _musical  # noqa: E402

for _k, _fn in _musical.QUERIES.items():
    QUERIES[_k] = (lambda f: (lambda s, p, a: f(s, p, a)))(_fn)
METHODS_DOC.update(_musical.HELP)
METHODS_DOC["analyze"] = "Render and measure {target: club|streaming|podcast|film, start, end, listen: true, reference: file.wav}"
QUERY_POSITIONAL = dict(_musical.POSITIONAL)


# ---- sound library + instrument kit (work without a project, except lib.use)
def _lib_paths(p):
    v = p.get("paths") or p.get("path") or []
    return [v] if isinstance(v, str) else list(v)


def _q_lib_use(s, p, author):
    from . import library
    if s is None:
        raise OpError("lib use needs a project (-p <folder>)")
    ops, reason = library.use_ops(s, p["id"], p.get("track"), p.get("at", "1.1.1"), p.get("as"))
    res = s.apply("batch", {"ops": ops}, author=author, reason=reason, label="lib.use")
    res.pop("results", None)
    return {**res, "reason": reason}


def _q_kit_install(s, p, author):
    from . import kit
    comps = p.get("components") or p.get("component")
    if isinstance(comps, str):
        comps = [c for c in re.split(r"[,\s]+", comps) if c]
    logs = []
    rep = kit.install(comps, log=(print if p.get("_cli") else logs.append), force=bool(p.get("force")))
    if logs:
        rep["log"] = logs
    return rep


GLOBAL_QUERIES = {
    "lib.scan": lambda s, p, a: __import__("plaindaw.library", fromlist=["x"]).scan(
        _lib_paths(p), tag=str(p.get("tag", True)).lower() not in ("false", "0", "no")),
    "lib.search": lambda s, p, a: __import__("plaindaw.library", fromlist=["x"]).search(
        " ".join(p["query"]) if isinstance(p.get("query"), list) else str(p.get("query", "")),
        p.get("kind"), p.get("engine"), p.get("key"), float(p["bpm"]) if p.get("bpm") else None, int(p.get("limit", 20))),
    "lib.list": lambda s, p, a: __import__("plaindaw.library", fromlist=["x"]).search(
        "", p.get("kind"), p.get("engine"), None, None, int(p.get("limit", 50))),
    "lib.info": lambda s, p, a: __import__("plaindaw.library", fromlist=["x"]).get(p["id"]),
    "lib.audition": lambda s, p, a: __import__("plaindaw.library", fromlist=["x"]).audition(
        p["id"], p.get("out"), p.get("note"), float(p["seconds"]) if p.get("seconds") else None),
    "kit.install": _q_kit_install,
    "kit.status": lambda s, p, a: __import__("plaindaw.kit", fromlist=["x"]).status(),
    "kit.list": lambda s, p, a: __import__("plaindaw.kit", fromlist=["x"]).status(),
    "kit.path": lambda s, p, a: {"kitDir": str(__import__("plaindaw.kit", fromlist=["x"]).kit_dir())},
    "kit.clean": lambda s, p, a: __import__("plaindaw.kit", fromlist=["x"]).clean(),
}
QUERIES.update(GLOBAL_QUERIES)
QUERIES["lib.use"] = _q_lib_use
QUERY_POSITIONAL.update({"lib.scan": ["paths*"], "lib.search": ["query*"], "lib.audition": ["id"], "lib.use": ["id"],
                         "lib.info": ["id"], "kit.install": ["components*"]})
METHODS_DOC.update({
    "lib.scan": "Index preset and sample folders (Surge .fxp, .vstpreset, .sfz, audio auto-tagged) {paths, tag}",
    "lib.search": "Search the sound library {query, kind sample|preset|instrument, engine, key, bpm, limit}",
    "lib.list": "List library items {kind, engine, limit}", "lib.info": "One library item {id}",
    "lib.audition": "Render a short preview of a preset or sample to a wav {id, note, out}",
    "lib.use": "Put a library item on a new track {id, track, at, as: audio|sampler}",
    "kit.install": "Download + verify the default kit: Surge XT, sfizz, CC0 piano/strings SFZ {components}",
    "kit.status": "What the instrument kit has installed", "kit.path": "Where the kit lives",
})


def _q_cli_exec(s, p, author):
    """Run one CLI command line against this project (used by the GUI's AI panel)."""
    import shlex
    from .cli import build_call, fmt_result
    line = str(p.get("line") or "").strip()
    words = shlex.split(line)
    if words and words[0] == "plaindaw":
        words = words[1:]
    if not words:
        raise OpError("type a command, e.g. `compose bass --bars 4`")
    if words[0] in ("new", "mcp", "rpc", "serve", "gui", "run", "plugins") or words[0].startswith("-"):
        raise OpError(f"{words[0]!r} is not available from the command box")
    method, params, flags = build_call(words)
    if method == "cli.exec":
        raise OpError("nested cli.exec")
    if flags["dry_run"]:
        params["dryRun"] = True
    if flags["confirm"]:
        params["confirm"] = True
    params.pop("_cli", None)
    res = s.call(method, params, author=author)
    return {"ok": True, "method": method, "text": fmt_result(method, res), "result": res}


QUERIES["cli.exec"] = _q_cli_exec
QUERY_POSITIONAL["cli.exec"] = ["line"]


# ---- live playback / live MIDI (render-ahead timeline + real-time live voices)
from . import live as _live  # noqa: E402

_live.register(QUERIES, METHODS_DOC, QUERY_POSITIONAL)
