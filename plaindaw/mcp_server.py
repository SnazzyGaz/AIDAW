"""MCP (Model Context Protocol) server over stdio, so AI assistants (Claude Desktop,
Cursor, Grok, etc.) can drive a PlainDAW project.

Self-contained implementation of the stdio transport (newline-delimited JSON-RPC 2.0),
compatible with MCP protocol versions 2024-11-05 .. 2025-11-25. Every edit goes
through the same op layer and undo history as the CLI and GUI, tagged author "ai".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__
from .model import ModelError
from .ops import OpError, op_catalog
from .session import METHODS_DOC, NeedsConfirm, PermissionDenied, Session

VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

INSTRUCTIONS = """PlainDAW: a plain-text DAW. One project = a folder with project.json.
Time: positions are 'bar.beat.16th' strings, 1-based ('1.1.1' = song start, '9.1.1' = bar 9),
or numbers of quarter notes from the start. Note times/lengths inside patterns are quarter notes
(1 = one beat in 4/4, 0.25 = a 16th). Keys: MIDI numbers (60 = C4) or names like 'F#3'.
Workflow: project_overview -> list_ops (once) -> edit / batch -> render (returns loudness,
true peak, band balance, per-section and per-track numbers, masking hints, warnings) -> adjust.
Use batch for multi-step changes so they undo as one step. Every change is in history with
author 'ai'; the person can undo it. Locked tracks/patterns refuse your edits: ask the person.
Built-ins: instruments builtin:synth|drums|sampler; effects builtin:eq|filter|compressor|limiter|
gate|reverb|delay|chorus|distortion|utility|volumeshaper. External: clap:<id>, vst3:<name> (see list_plugins),
sfz:<kit set|file.sfz>. Kit (after kit_install): Surge XT and sfizz, used by name (instrument_add surge|sfizz).
Listening: `listen` gives key, chords per bar, tempo/beat grid, timing tightness, transients, per-track
timbre tags, section energy and (with reference=file) plain-English differences from a reference.
Musical commands compile to ordinary ops as ONE undo step with a logged reason: compose (bass, chords,
pad, lead, arp, drums, song), arrange (form), fix_clash (before/after numbers), variations (try N,
score by a metric, keep the best), humanize, preview (quick render of bars/tracks). Pass `reason`
to edit/batch so the person can see why you changed something."""


def _obj(props: dict, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}
B = {"type": "boolean"}

TOOLS = [
    ("project_overview", "Compact overview: tempo, key, length, markers, tracks (mix, plugins, sends), patterns, locks.",
     _obj({}), lambda s, a: s.summary()),
    ("get_project", "The full project JSON (large). Prefer project_overview / get_pattern.", _obj({}), lambda s, a: s.doc),
    ("list_ops", "Every edit op with its arguments and which are destructive.", _obj({}), lambda s, a: op_catalog()),
    ("edit", "Apply one edit op, e.g. op='mix.set' args={'track':'bass','gainDb':-3}. dryRun previews changes. "
             "reason is stored in the history.",
     _obj({"op": S, "args": {"type": "object"}, "dryRun": B, "confirm": B, "reason": S}, ["op"]),
     lambda s, a: s.apply(a["op"], dict(a.get("args") or {}), author="ai", dry_run=bool(a.get("dryRun")),
                          confirm=bool(a.get("confirm")), reason=a.get("reason"))),
    ("batch", "Apply several ops as ONE undo step: ops=[{op, args}, ...]. reason is stored in the history.",
     _obj({"ops": {"type": "array", "items": {"type": "object"}}, "dryRun": B, "confirm": B, "reason": S}, ["ops"]),
     lambda s, a: s.apply("batch", {"ops": a["ops"]}, author="ai", dry_run=bool(a.get("dryRun")),
                          confirm=bool(a.get("confirm")), reason=a.get("reason"))),
    ("get_pattern", "Notes of a pattern with note names.", _obj({"pattern": S}, ["pattern"]),
     lambda s, a: s.pattern_text(a["pattern"])),
    ("render", "Render the song (or start..end) to audio files in renders/ and return the analysis. "
               "formats like 'wav,mp3'; target club|streaming|podcast|film checks loudness.",
     _obj({"formats": S, "bits": S, "start": S, "end": S, "stems": B, "target": S, "name": S}),
     lambda s, a: s.export(formats=a.get("formats", "wav"), bits=str(a.get("bits", "24")), start=a.get("start"),
                           end=a.get("end"), stems=bool(a.get("stems")), target=a.get("target"), name=a.get("name"))),
    ("analyze", "Render in memory and measure (no files): LUFS, true peak, LRA, bands, stereo, sections, per-track, masking.",
     _obj({"target": S, "start": S, "end": S}),
     lambda s, a: s.analyze(target=a.get("target"), start=a.get("start"), end=a.get("end"))),
    ("undo", "Undo the most recent edit.", _obj({}), lambda s, a: s.undo("ai")),
    ("redo", "Redo the last undone edit.", _obj({}), lambda s, a: s.redo("ai")),
    ("revert", "Undo one specific earlier edit by its history number, if nothing later touched the same data.",
     _obj({"n": I}, ["n"]), lambda s, a: s.revert(a["n"], "ai")),
    ("history", "Recent edits (who, what).", _obj({"limit": I}), lambda s, a: s.history(int(a.get("limit", 20)))),
    ("diff", "Musical diff of what changed since history entry n.", _obj({"since": I}),
     lambda s, a: s.call("diff", {"since": a.get("since", 0)})),
    ("list_plugins", "Built-in and installed VST3/CLAP plugins.", _obj({}), lambda s, a: s.call("plugin.scan", {})),
    ("plugin_params", "Parameters of a plugin: plugin='builtin:compressor' or 'clap:<id>' or track (+insert).",
     _obj({"plugin": S, "track": S, "insert": S}), lambda s, a: s.call("plugin.params", dict(a))),
    ("export_midi", "Write the instrument tracks to a .mid file (for FL Studio etc.).", _obj({"out": S}),
     lambda s, a: s.call("export.midi", dict(a))),
    ("listen", "Listen to the mix (or start..end, or some tracks): key + runner-up, chords per bar, tempo and beat grid, "
               "timing vs the 1/16 grid, transient strength, timbre tags per track (bright, muddy, harsh, wide...), "
               "energy per bar and per section, summary lines. reference='file.wav' adds plain-English differences.",
     _obj({"start": S, "end": S, "reference": S, "tracks": {"type": "array", "items": S}}),
     lambda s, a: _slim(s.call("listen", dict(a), author="ai"))),
    ("compose", "Write a part as one undo step. part: bass|chords|pad|lead|arp|drums|song. key ('F minor'), chords "
                "('i VI III VII' or 'Fm Db Ab Eb'), bars, genre (bigroom, house, techno, dnb, trap, hiphop, lofi, ambient, "
                "horror), style (part style e.g. offbeat, reese, 808, stabs, keys, pluck, anthem, bell), seed, at, track, "
                "engine auto|builtin|surge|sfz, preset. part=song also sets tempo/key, mixes and arranges (form).",
     _obj({"part": S, "key": S, "chords": S, "bars": I, "genre": S, "style": S, "seed": I, "at": S, "track": S,
           "engine": S, "preset": S, "form": S, "bpm": N, "swing": N, "dryRun": B}, ["part"]),
     lambda s, a: s.call("compose", dict(a), author="ai")),
    ("arrange", "Lay composed parts out into a form 'intro:8,build:8,drop:16,break:8,drop:16,outro:8' with markers, "
                "snare rolls, risers and crashes (fx=false to skip).", _obj({"form": S, "fx": B, "dryRun": B}),
     lambda s, a: s.call("arrange", dict(a), author="ai")),
    ("fix_clash", "Measure how two tracks mask each other (clash index per band), apply sidechain and/or EQ, re-measure, "
                  "and keep it only if it helped. mode auto|sidechain|eq. Range defaults to the loop.",
     _obj({"a": S, "b": S, "mode": S, "start": S, "end": S, "bars": S, "amount": N, "dryRun": B, "force": B}, ["a", "b"]),
     lambda s, a: s.call("fix.clash", dict(a), author="ai")),
    ("clash_report", "Read-only clash index for each pair of the loudest stems.", _obj({"start": S, "end": S, "top": I}),
     lambda s, a: s.call("clash.report", dict(a), author="ai")),
    ("variations", "Try n versions of a command on scratch copies (seeds, or vary='name=lo..hi'), render a short range, "
                   "score by target (loudness, punch, brightness, darkness, width, clarity, keyfit, energy, contrast, "
                   "tightness; 'loudness=-9' aims at a value), keep the best (keep=best) or just list (keep=list).",
     _obj({"n": I, "command": S, "target": S, "vary": S, "keep": S, "start": S, "end": S}, ["command"]),
     lambda s, a: s.call("variations", dict(a), author="ai")),
    ("humanize", "Loosen a track's timing/velocity (feel tight|natural|loose|swing|drunk, amount, swing, seed).",
     _obj({"track": S, "pattern": S, "feel": S, "amount": N, "swing": N, "seed": I, "dryRun": B}),
     lambda s, a: s.call("humanize", dict(a), author="ai")),
    ("preview", "Quick render of bars ('9-12' or a section name) and/or soloed tracks to renders/preview.wav with a short "
                "listen summary.", _obj({"bars": S, "start": S, "end": S, "tracks": {"type": "array", "items": S}, "sr": I}),
     lambda s, a: s.call("preview", dict(a), author="ai")),
    ("instrument_add", "Add an instrument track by name: surge (Surge XT, optional preset), sfizz (sfz=piano|strings|file), "
                       "synth, drums, sampler, or a clap:/vst3: ref.",
     _obj({"plugin": S, "name": S, "preset": S, "sfz": S, "role": S}, ["plugin"]),
     lambda s, a: s.call("instrument.add", dict(a), author="ai")),
    ("lib_search", "Search the sound library (presets, SFZ, auto-tagged samples): query words, kind, engine, key, bpm.",
     _obj({"query": S, "kind": S, "engine": S, "key": S, "bpm": N, "limit": I}),
     lambda s, a: s.call("lib.search", dict(a), author="ai")),
    ("lib_scan", "Index folders of presets/samples (auto-tags samples by type, key, tempo, brightness).",
     _obj({"paths": {"type": "array", "items": S}, "tag": B}), lambda s, a: s.call("lib.scan", dict(a), author="ai")),
    ("lib_audition", "Render a short phrase through a preset (or trim a sample) to a wav and say what it sounds like.",
     _obj({"id": S, "note": S}, ["id"]), lambda s, a: s.call("lib.audition", dict(a), author="ai")),
    ("lib_use", "Put a library item on a new track (as=audio|sampler for samples).",
     _obj({"id": S, "track": S, "at": S, "as": S}, ["id"]), lambda s, a: s.call("lib.use", dict(a), author="ai")),
    ("kit_status", "Which kit instruments (Surge XT, sfizz, SFZ sets) are installed.", _obj({}),
     lambda s, a: s.call("kit.status", {}, author="ai")),
]


def _slim(rep):
    """Listen reports are big; keep what an assistant needs."""
    if isinstance(rep, dict):
        rep = {k: v for k, v in rep.items() if k not in ("beatGrid",)}
        if isinstance(rep.get("timbre"), dict):
            rep["timbre"] = {k: {"tags": v.get("tags"), "centroidHz": v.get("centroidHz")} for k, v in rep["timbre"].items()}
    return rep
TOOL_MAP = {t[0]: t for t in TOOLS}


class Server:
    def __init__(self, session: Session):
        self.s = session
        self.version = VERSIONS[-1]

    def handle(self, msg: dict) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method")
        p = msg.get("params") or {}
        if method is None:  # a response to something we never send
            return None
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                want = p.get("protocolVersion")
                self.version = want if want in VERSIONS else VERSIONS[-1]
                return self._ok(mid, {"protocolVersion": self.version,
                                      "capabilities": {"tools": {"listChanged": False}},
                                      "serverInfo": {"name": "plaindaw", "version": __version__},
                                      "instructions": INSTRUCTIONS})
            if method == "ping":
                return self._ok(mid, {})
            if method == "tools/list":
                return self._ok(mid, {"tools": [{"name": n, "description": d, "inputSchema": sch} for n, d, sch, _ in TOOLS]})
            if method == "tools/call":
                return self._ok(mid, self.call_tool(p.get("name"), p.get("arguments") or {}))
            if method in ("resources/list", "prompts/list"):
                return self._ok(mid, {"resources" if method.startswith("res") else "prompts": []})
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}

    def call_tool(self, name, args) -> dict:
        t = TOOL_MAP.get(name)
        if t is None:
            return self._text(f"unknown tool {name!r}", True)
        try:
            res = t[3](self.s, args)
            return self._text(json.dumps(res, ensure_ascii=False, indent=1), False)
        except NeedsConfirm as e:
            return self._text(f"NEEDS CONFIRMATION: {e}", True)
        except PermissionDenied as e:
            return self._text(f"REFUSED: {e}", True)
        except (OpError, ModelError, ValueError, KeyError, FileNotFoundError) as e:
            return self._text(f"error: {e if not isinstance(e, KeyError) else 'missing ' + str(e)}", True)

    @staticmethod
    def _text(t, err):
        return {"content": [{"type": "text", "text": t}], "isError": err}

    @staticmethod
    def _ok(mid, res):
        return {"jsonrpc": "2.0", "id": mid, "result": res}


def serve(session: Session, inp=None, out=None) -> int:
    inp = inp or sys.stdin
    out = out or sys.stdout
    srv = Server(session)
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            out.flush()
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        replies = [r for r in (srv.handle(m) for m in msgs if isinstance(m, dict)) if r is not None]
        for r in replies:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
        out.flush()
    return 0


def main(project_dir: Path, mode: str = "allow") -> int:
    # keep stdout clean for protocol traffic
    real_out = sys.stdout
    sys.stdout = sys.stderr
    try:
        s = Session(project_dir, author="ai", mode=mode)
        return serve(s, sys.stdin, real_out)
    finally:
        sys.stdout = real_out
