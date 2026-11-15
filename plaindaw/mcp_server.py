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
gate|reverb|delay|chorus|distortion|utility|volumeshaper. External: clap:<id>, vst3:<name> (see list_plugins)."""


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
    ("edit", "Apply one edit op, e.g. op='mix.set' args={'track':'bass','gainDb':-3}. dryRun previews changes.",
     _obj({"op": S, "args": {"type": "object"}, "dryRun": B, "confirm": B}, ["op"]),
     lambda s, a: s.apply(a["op"], dict(a.get("args") or {}), author="ai", dry_run=bool(a.get("dryRun")),
                          confirm=bool(a.get("confirm")))),
    ("batch", "Apply several ops as ONE undo step: ops=[{op, args}, ...].",
     _obj({"ops": {"type": "array", "items": {"type": "object"}}, "dryRun": B, "confirm": B}, ["ops"]),
     lambda s, a: s.apply("batch", {"ops": a["ops"]}, author="ai", dry_run=bool(a.get("dryRun")),
                          confirm=bool(a.get("confirm")))),
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
]
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
