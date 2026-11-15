"""Command line: plaindaw [-p PROJECT] <noun> <verb> [positional...] [--arg value ...]

Examples
  plaindaw new mysong --bpm 128 --title "My Song"
  plaindaw -p mysong track add Bass --plugin builtin:synth
  plaindaw -p mysong pattern add bassline --lengthBeats 16
  plaindaw -p mysong pattern add_notes bassline --notes "F2@0:0.5 F2@0.5:0.5 Ab2@1:1:110"
  plaindaw -p mysong mix set bass --gain -3 --pan -0.2
  plaindaw -p mysong render --formats wav,mp3
  plaindaw -p mysong analyze --target club
  plaindaw -p mysong undo | redo | history | summary | ops
  plaindaw -p mysong run script.pdaw      (one command per line)
  plaindaw -p mysong play [5.1.1] | stop | seek 9.1.1 | status   (live playback)
  plaindaw -p mysong transport loop on | transport metronome on | devices
  plaindaw -p mysong serve [--port 8765]  (browser GUI + JSON-RPC over HTTP)
  plaindaw -p mysong rpc                  (JSON-RPC 2.0 over stdio)
  plaindaw -p mysong mcp                  (MCP server over stdio for AI assistants)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

from . import __version__
from .ops import REGISTRY, OpError
from .model import ModelError

ALIASES = {"gain": "gainDb", "level": "levelDb", "length": "lengthBeats", "len": "lengthBeats", "vol": "gainDb",
           "volume": "gainDb", "type_": "type", "sr": "sampleRate", "format": "formats"}

SHORT = {  # "noun verb" shortcuts that are not ops
    "undo": "history.undo", "redo": "history.redo", "history": "history.list", "summary": "project.summary",
    "info": "project.summary", "ops": "ops.list", "render": "render", "export": "render", "analyze": "analyze",
    "diff": "diff", "validate": "project.validate", "get": "project.get", "revert": "history.revert",
    "play": "transport.play", "stop": "transport.stop", "seek": "transport.seek", "status": "transport.status",
    "metronome": "transport.metronome", "devices": "audio.devices",
}

NOTE_RE = re.compile(r"^([A-Ga-g][#b]?-?\d+|\d+)@([\d.]+)(?::([\d.]+))?(?::(\d+))?$")


def parse_notes(text: str) -> list:
    """'C4@0:1:100 E4@1:0.5' -> [{key, t, len, vel}] (t/len in quarter notes)."""
    out = []
    for tok in text.replace(",", " ").split():
        m = NOTE_RE.match(tok)
        if not m:
            raise OpError(f"bad note {tok!r}: use KEY@START[:LEN[:VEL]], e.g. C4@0:1:100")
        k = int(m.group(1)) if m.group(1).isdigit() else m.group(1)
        n = {"key": k, "t": float(m.group(2)), "len": float(m.group(3) or 1)}
        if m.group(4):
            n["vel"] = int(m.group(4))
        out.append(n)
    return out


def parse_points(text: str) -> list:
    """'1.1.1=-12 5.1.1=0' -> [["1.1.1", -12], ["5.1.1", 0]]"""
    out = []
    for tok in text.replace(",", " ").split():
        if "=" not in tok:
            raise OpError(f"bad point {tok!r}: use POSITION=VALUE, e.g. 5.1.1=-6")
        a, b = tok.split("=", 1)
        out.append([a, float(b)])
    return out


def coerce(spec_type: str | None, name: str, raw: str):
    if spec_type in ("position", "string", None) and not (spec_type is None and raw[:1] in "[{"):
        if spec_type is None:
            try:
                return json.loads(raw)
            except ValueError:
                return raw
        return raw
    if spec_type == "number":
        try:
            return float(raw)
        except ValueError:
            raise OpError(f"--{name} must be a number")
    if spec_type == "integer":
        try:
            return int(raw)
        except ValueError:
            raise OpError(f"--{name} must be a whole number")
    if spec_type == "boolean":
        if raw.lower() in ("1", "true", "yes", "on", "y"):
            return True
        if raw.lower() in ("0", "false", "no", "off", "n"):
            return False
        raise OpError(f"--{name} must be true or false")
    if spec_type == "array":
        if raw.strip().startswith("["):
            return json.loads(raw)
        if name == "notes":
            return parse_notes(raw)
        if name == "points":
            return parse_points(raw)
        if name in ("indices",):
            return [int(x) for x in raw.replace(",", " ").split()]
        return [x for x in raw.replace(",", " ").split()]
    if spec_type == "object":
        if raw.strip().startswith("{"):
            return json.loads(raw)
        obj = {}
        for kv in raw.replace(",", " ").split():
            if "=" not in kv:
                raise OpError(f"--{name}: use k=v pairs or JSON")
            k, v = kv.split("=", 1)
            try:
                obj[k] = json.loads(v)
            except ValueError:
                obj[k] = v
        return obj
    return raw


def parse_tokens(tokens: list[str]):
    pos, kw = [], {}
    flags = {"json": False, "dry_run": False, "confirm": False}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("--json",):
            flags["json"] = True
        elif t in ("--dry-run", "--dryRun"):
            flags["dry_run"] = True
        elif t == "--confirm":
            flags["confirm"] = True
        elif t.startswith("--") and len(t) > 2:
            k = t[2:]
            if "=" in k:
                k, v = k.split("=", 1)
            elif i + 1 < len(tokens) and not (tokens[i + 1].startswith("--") and len(tokens[i + 1]) > 2):
                v = tokens[i + 1]; i += 1
            else:
                v = "true"
            kw[k] = v
        else:
            pos.append(t)
        i += 1
    return pos, kw, flags


def build_call(words: list[str]):
    """Turn CLI words into (method, params, flags)."""
    pos, kw, flags = parse_tokens(words)
    if not pos:
        raise OpError("no command given (try: plaindaw --help)")
    method = None
    if len(pos) >= 2 and f"{pos[0]}.{pos[1]}" in REGISTRY:
        method, rest = f"{pos[0]}.{pos[1]}", pos[2:]
    elif len(pos) >= 2 and f"{pos[0]}.{pos[1].replace('-', '_')}" in REGISTRY:
        method, rest = f"{pos[0]}.{pos[1].replace('-', '_')}", pos[2:]
    elif "." in pos[0] and pos[0] in REGISTRY:
        method, rest = pos[0], pos[1:]
    if method:
        spec = REGISTRY[method]
        params = {}
        for k, v in kw.items():
            if k not in spec.args and ALIASES.get(k) in spec.args:
                k = ALIASES[k]
            params[k] = coerce(spec.args.get(k, (None,))[0], k, v)
        for i, v in enumerate(rest):
            if i >= len(spec.positional):
                raise OpError(f"{method}: too many positional values ({rest}); positional order: {spec.positional}")
            k = spec.positional[i]
            if k in params:
                raise OpError(f"{method}: '{k}' given twice")
            params[k] = coerce(spec.args[k][0], k, v)
        return method, params, flags
    from .session import QUERIES
    if len(pos) >= 2 and f"{pos[0]}.{pos[1]}" in QUERIES:
        q, used = f"{pos[0]}.{pos[1]}", 2
    elif SHORT.get(pos[0]) or pos[0] in QUERIES:
        q, used = SHORT.get(pos[0]) or pos[0], 1
    else:
        raise OpError(f"unknown command {' '.join(pos[:2])!r} (try: plaindaw ops)")
    rest = pos[used:]
    params = {}
    for k, v in kw.items():
        k = {"format": "formats"}.get(k, k)
        try:
            params[k] = json.loads(v)
        except ValueError:
            params[k] = v
    if q in ("render", "analyze") and "formats" in params and isinstance(params["formats"], str):
        params["formats"] = params["formats"]
    from .session import QUERY_POSITIONAL
    if rest and q in QUERY_POSITIONAL:
        names = QUERY_POSITIONAL[q]
        if names and names[-1].endswith("*"):  # trailing list argument
            star = names[-1][:-1]
            fixed = names[:-1]
            for nm, v in zip(fixed, rest):
                params[nm] = v
            if len(rest) > len(fixed):
                params[star] = rest[len(fixed):]
            rest = []
        for i, v in enumerate(rest):
            if i >= len(names):
                raise OpError(f"{q}: too many positional values {rest}; order: {names}")
            if names[i] in params:
                raise OpError(f"{q}: '{names[i]}' given twice")
            try:
                params[names[i]] = json.loads(v) if names[i] in ("n",) else v
            except ValueError:
                params[names[i]] = v
        rest = []
    if rest:
        key = {"history.revert": "n", "pattern.get": "pattern", "import.midi": "path", "import.stems": "paths",
               "plugin.params": "plugin", "history.list": "limit", "diff": "since"}.get(q)
        if key is None:
            raise OpError(f"{q} takes --options only, got {rest}")
        params[key] = rest if q == "import.stems" else rest[0]
    return q, params, flags


MUSICAL_SHOW = ("key", "chords", "bars", "tracks", "sounds", "sections", "actions", "before", "after", "applied", "note",
                 "metric", "target", "results", "best", "beforeMs", "afterMs", "path", "durationSec", "renderSec", "listen",
                 "instrument", "track", "range", "pairs", "hint")


def _fmt_musical(method, res) -> str:
    head = f"#{res['n']} {res.get('op', method)}" if res.get("n") else (
        f"(dry run) {method}" if res.get("dryRun") else method)
    lines = [head]
    if res.get("reason"):
        lines.append(f"  why: {res['reason']}")
    for k in MUSICAL_SHOW:
        if k not in res or res[k] in (None, [], {}):
            continue
        v = res[k]
        if k == "sections":
            v = ", ".join(f"{x['name']}@{x['bar']}({x['bars']})" for x in v)
        elif k == "pairs":
            v = "; ".join(f"{r['a']}+{r['b']} {r['index']} (worst {r['worstBand']} Hz)" for r in v[:6])
        elif k == "results":
            v = "; ".join(f"{r['label']}: {r.get('value', r.get('error'))}" for r in v)
        elif k in ("before", "after"):
            v = f"clash index {v['index']} (worst {v['worstBand']} Hz {v['worstPct']}%)"
        elif k == "listen":
            v = " | ".join(v.get("summary") or []) or json.dumps(v)
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        lines.append(f"  {k}: {v}")
    for c in (res.get("changes") or [])[:8]:
        lines.append("  " + c)
    if res.get("arrange"):
        lines.append(_fmt_musical("arrange", res["arrange"]))
    return "\n".join(lines)


def fmt_result(method, res) -> str:
    from .musical import QUERIES as MQ
    if method.startswith(("lib.", "kit.")) and method != "lib.use":
        return fmt_global(method, res)
    if method.startswith(("transport.", "audio.set", "midi.open", "midi.close", "midi.target")):
        return _fmt_transport(res)
    if (method in MQ or method in ("listen", "lib.use")) and isinstance(res, dict):
        if method == "listen" and res.get("text"):
            return res["text"]
        return _fmt_musical(method, res)
    if isinstance(res, dict) and "changes" in res and res.get("ok"):
        head = f"#{res['n']} {method}" if res.get("n") else ("(dry run) " + method if res.get("dryRun") else f"{method} (no change)")
        extra = {k: v for k, v in res.items() if k not in ("ok", "n", "op", "changes", "dryRun", "changed")}
        lines = [head + (" " + json.dumps(extra) if extra else "")] + ["  " + c for c in res["changes"]]
        return "\n".join(lines)
    if method == "history.undo":
        return f"undid #{res['undid']} {res['op']} (by {res['by']})"
    if method == "history.redo":
        return f"redid #{res['redid']} {res['op']}"
    if method == "history.revert":
        return f"reverted #{res['reverted']} (as #{res['n']})"
    if method == "history.list":
        out = []
        for e in res:
            mark = {"undone": " (undone)", "applied": ""}.get(e["state"], "")
            out.append(f"#{e['n']:<4} {e['author']:<6} {e['op']:<20} {'; '.join(e['summary'][:2])}{mark}")
            if e.get("reason"):
                out.append(f"      why: {e['reason']}")
        return "\n".join(out) or "(no history)"
    if method == "ops.list":
        return "\n".join(f"{o['op']:<22} {o['summary']}" + ("  [destructive]" if o["destructive"] else "") for o in res)
    if method in ("render", "export.audio"):
        out = [f"rendered {res['seconds']} s"] + [f"  {f['path']}" for f in res["files"]]
        if res.get("analysis"):
            out.append(fmt_analysis(res["analysis"]))
        return "\n".join(out)
    if method == "analyze":
        return fmt_analysis(res) + (("\n\n" + res["listen"]["text"]) if res.get("listen", {}).get("text") else "")
    return json.dumps(res, indent=2, ensure_ascii=False)


def fmt_global(method, res) -> str:
    if method in ("lib.search", "lib.list"):
        if not res:
            return "(no matches)"
        return "\n".join(f"{r['id']}  {r['kind']:<10} {r['name'][:36]:<36} {', '.join(r.get('tags', [])[:6]):<40}"
                         + (f" key {r['key']}" if r.get("key") else "") + (f" {r['bpm']} bpm" if r.get("bpm") else "")
                         for r in res)
    if method == "lib.audition":
        return "\n".join([f"audition {res['name']} -> {res['path']} ({res['seconds']} s, rendered in {res['renderSec']} s)"]
                         + [f"  heard: {h}" for h in res.get("heard", [])] + [f"  warning: {w}" for w in res.get("warnings", [])])
    if method == "lib.scan":
        return (f"indexed {res['total']} items ({', '.join(f'{v} {k}' for k, v in res['byKind'].items())}); "
                f"{res['added']} new, {res['updated']} updated, {res['unchanged']} unchanged, {res['errors']} errors "
                f"in {res['seconds']} s -> {res['index']}")
    if method == "kit.install":
        st = res["status"]
        lines = [f"kit: {res['kitDir']}", f"  Surge XT: {'ok' if st['surge'] else 'missing'}",
                 f"  sfizz: {'ok' if st['sfizz'] else 'missing'}", f"  SFZ sets: {', '.join(st['sfz']) or 'none'}"]
        lines += [f"  failed {k}: {v}" for k, v in res.get("failed", {}).items()]
        return "\n".join(lines)
    return json.dumps(res, indent=2, ensure_ascii=False)


def fmt_analysis(a) -> str:
    m = a["master"]
    lines = [f"loudness {m['integratedLufs']} LUFS (short-term max {m['shortTermMaxLufs']}, LRA {m['lra']})",
             f"true peak {m['truePeakDbtp']} dBTP, sample peak {m['samplePeakDbfs']} dBFS, crest {m['crestDb']} dB",
             f"stereo correlation {m['stereoCorrelation']}, width {m['stereoWidth']}, low-end corr {m['lowEndCorrelation']}",
             "bands (dB rel. total): " + ", ".join(f"{k.split('_')[0]} {v}" for k, v in a["bandsDbRel"].items()),
             f"spectral centroid {a['spectralCentroidHz']} Hz"]
    for s in a.get("sections", []):
        lines.append(f"  section {s['name']:<10} {s['integratedLufs']} LUFS  peak {s['peakDbfs']} dBFS")
    if a.get("target"):
        t = a["target"]
        lines.append(f"target {t['profile']}: {'PASS' if t['pass'] else 'FAIL'} - " +
                     "; ".join(c.get("detail") or f"{c['check']} {c['value']} (max {c['target']})" for c in t["checks"]))
    for w in a.get("warnings", []) + a.get("renderWarnings", []):
        lines.append(f"warning: {w}")
    return "\n".join(lines)


def find_project(p: str | None) -> Path:
    if p:
        return Path(p)
    if os.environ.get("PLAINDAW_PROJECT"):
        return Path(os.environ["PLAINDAW_PROJECT"])
    cur = Path.cwd()
    for d in [cur, *cur.parents]:
        if (d / "project.json").exists():
            return d
    raise OpError("no project: pass -p <folder>, set PLAINDAW_PROJECT, or cd into a project folder")


def _fmt_transport(res) -> str:
    if not isinstance(res, dict) or "posQn" not in res:
        return json.dumps(res, ensure_ascii=False, indent=2)
    r = res.get("render", {})
    lines = [f"{'playing' if res['playing'] else 'stopped'} at {res['pos']} ({res['bpm']:g} BPM)"
             + (" · loop" if res.get("loop") else "") + (" · click" if res.get("metronome") else "")
             + (" · REC" if res.get("record") else ""),
             f"  render: {r.get('state')}" + (f", {r['renderSec']} s ({r.get('cachedParts', 0)} cached, "
                                               f"{r.get('renderedParts', 0)} re-rendered)" if 'renderSec' in r else ""),
             f"  audio: {res['output']} · device {res.get('deviceName') or res.get('device') or 'default'} · {res['sampleRate']} Hz · "
             f"buffer {res['blocksize']} · latency {res['latencyMs']} ms · cpu {res['cpu']}%"
             + (f" · served by the GUI" if res.get("where") == "server" else "")]
    if res.get("midiPort"):
        lines.append(f"  midi: {res['midiPort']} -> {res.get('midiTrack')}")
    if res.get("error"):
        lines.append(f"  note: {res['error']}")
    return "\n".join(lines)


def run_line(session, words, as_author, out, force_json=False) -> int:
    method, params, flags = build_call(words)
    if flags["dry_run"]:
        params["dryRun"] = True
    if flags["confirm"]:
        params["confirm"] = True
    res = session.call(method, params, author=as_author)
    out.write((json.dumps(res, ensure_ascii=False) if (flags["json"] or force_json) else fmt_result(method, res)) + "\n")
    if method == "transport.play" and isinstance(res, dict) and res.get("where") == "local" and out is sys.stdout:
        _play_here(session, out)
    return 0


def _play_here(session, out) -> None:
    """`plaindaw play` with no GUI server running: play in this process until the song ends
    (or forever when looping) and show the playhead; Ctrl+C stops."""
    import time
    from .live import engine_for
    e = engine_for(session)
    try:
        while e.playing:
            st = e.status()
            out.write(f"\r  {st['pos']:<12} {st['render']['state']:<10} cpu {st['cpu']:>4}%  "
                      f"latency {st['latencyMs']} ms  {'loop' if st['loop'] else ''}   ")
            out.flush()
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    e.stop()
    out.write("\n")
    e.close()


def _utf8_streams(argv) -> None:
    """Windows consoles default to cp1252: never crash on characters like '→'. Pipes (MCP, RPC,
    scripts) get UTF-8; an interactive console keeps its encoding and replaces what it can't show."""
    machine = "mcp" in argv or "rpc" in argv
    for name in ("stdout", "stderr", "stdin"):
        st = getattr(sys, name, None)
        if st is None or not hasattr(st, "reconfigure") or (st.encoding or "").lower().replace("-", "") == "utf8":
            continue
        try:
            if machine or not st.isatty():
                st.reconfigure(encoding="utf-8", errors="replace" if name != "stdin" else "strict")
            elif name != "stdin":
                st.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def main(argv=None) -> int:
    if argv is None:
        _utf8_streams(sys.argv[1:])
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] in ("--version", "version"):
        print(f"plaindaw {__version__}")
        return 0
    proj, author, mode = None, "human", "allow"
    while argv and argv[0] in ("-p", "--project", "--as", "--mode"):
        flag = argv.pop(0)
        if not argv:
            print(f"error: {flag} needs a value", file=sys.stderr); return 2
        v = argv.pop(0)
        if flag in ("-p", "--project"):
            proj = v
        elif flag == "--as":
            author = v
        else:
            mode = v
    try:
        if argv[0] == "new":
            from .session import create_project
            pos, kw, flags = parse_tokens(argv[1:])
            if not pos:
                raise OpError("usage: plaindaw new <folder> [--title T] [--bpm 120] [--sig 4/4] [--sr 48000]")
            num, den = (kw.get("sig") or "4/4").split("/")
            p = create_project(Path(pos[0]), kw.get("title", Path(pos[0]).name), float(kw.get("bpm", 120)),
                               int(num), int(den), int(kw.get("sampleRate", 48000)), kw.get("author", ""))
            print(f"created {p}")
            return 0
        if argv[0] == "plugins":
            from .session import _scan
            pos, kw, flags = parse_tokens(argv[1:])
            print(json.dumps(_scan(kw), indent=2))
            return 0
        from .session import Session, GLOBAL_QUERIES
        if argv[0] in ("kit", "lib"):
            try:
                pdir = find_project(proj)
            except OpError:
                pdir = None
            method, params, flags = build_call(argv)
            if method in GLOBAL_QUERIES or pdir is None:
                from .session import QUERIES
                if method == "kit.install" and not flags["json"]:
                    params["_cli"] = True
                sess = Session(pdir, author=author, mode=mode) if pdir is not None else None
                res = QUERIES[method](sess, params, author)
                print(json.dumps(res, ensure_ascii=False) if flags["json"] else fmt_global(method, res))
                return 0
        if argv[0] == "mcp":
            from .mcp_server import main as mcp_main
            return mcp_main(find_project(proj), mode=mode if mode != "allow" else os.environ.get("PLAINDAW_MODE", "allow"))
        session = Session(find_project(proj), author=author, mode=mode)
        if argv[0] == "rpc":
            from .rpc import serve_stdio
            return serve_stdio(session)
        if argv[0] in ("serve", "gui"):
            from .rpc import serve_http
            pos, kw, flags = parse_tokens(argv[1:])
            return serve_http(session, host=kw.get("host", "127.0.0.1"), port=int(kw.get("port", 8765)),
                              open_browser=argv[0] == "gui")
        if argv[0] == "run":
            pos, kw, flags = parse_tokens(argv[1:])
            if not pos:
                raise OpError("usage: plaindaw run <script> (one command per line, # comments)")
            text = sys.stdin.read() if pos[0] == "-" else Path(pos[0]).read_text(encoding="utf-8")
            for ln, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                words = shlex.split(line)
                if words and words[0] == "plaindaw":
                    words = words[1:]
                try:
                    if not flags["json"]:
                        print(f"> {line}")
                    run_line(session, words, author, sys.stdout, force_json=flags["json"])
                except (OpError, ModelError, ValueError, KeyError) as e:
                    print(f"error on line {ln}: {e}", file=sys.stderr)
                    return 1
            return 0
        return run_line(session, argv, author, sys.stdout)
    except (OpError, ModelError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"error: missing {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
