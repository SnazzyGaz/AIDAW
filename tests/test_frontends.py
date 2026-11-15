import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(ROOT)}


def cli(*args, cwd=None, input=None):
    return subprocess.run([sys.executable, "-m", "plaindaw", *args], capture_output=True, text=True, cwd=cwd,
                          env=ENV, input=input, timeout=120)


def test_cli_end_to_end(tmp_path):
    p = str(tmp_path / "song")
    assert cli("new", p, "--bpm", "128", "--title", "CLI").returncode == 0
    steps = [
        ["track", "add", "Lead", "--plugin", "builtin:synth", "--params", "wave=square voices=3"],
        ["clip", "add_notes", "lead", "1.1.1", "--notes", "C4@0:1 E4@1:1:90 G4@2:2"],
        ["mix", "set", "lead", "--gain", "-3", "--pan", "0.25"],
        ["plugin", "add", "lead", "builtin:delay", "--params", '{"timeBeats": 0.5, "mix": 0.2}'],
        ["auto", "set", "lead", "mix/gainDb", "--points", "1.1.1=-12 2.1.1=-3"],
    ]
    for st in steps:
        r = cli("-p", p, *st)
        assert r.returncode == 0, r.stderr
    r = cli("-p", p, "summary", "--json")
    s = json.loads(r.stdout)
    lead = next(t for t in s["tracks"] if t["id"] == "lead")
    assert lead["gainDb"] == -3 and lead["pan"] == 0.25 and lead["inserts"] == ["lead_delay=builtin:delay"]
    r = cli("-p", p, "mix", "set", "lead", "--gain", "500")
    assert r.returncode == 1 and "out of range" in r.stderr
    assert cli("-p", p, "undo").returncode == 0
    r = cli("-p", p, "history", "--json")
    assert json.loads(r.stdout)[-1]["op"] == "history.undo"
    r = cli("-p", p, "render", "--formats", "wav,mp3", "--target", "streaming")
    assert r.returncode == 0, r.stderr
    assert "loudness" in r.stdout and "target streaming" in r.stdout
    assert (Path(p) / "renders" / "CLI.wav").exists() and (Path(p) / "renders" / "CLI.mp3").exists()
    # dry run and cwd project discovery
    r = cli("track", "remove", "lead", "--dry-run", cwd=p)
    assert r.returncode == 0 and "dry run" in r.stdout
    assert json.loads(cli("track", "list", "--json", cwd=p).stdout)[0]["id"] == "lead"


def test_cli_run_script(tmp_path):
    p = str(tmp_path / "s")
    cli("new", p)
    script = tmp_path / "x.pdaw"
    script.write_text("# comment\ntrack add Kick --plugin builtin:drums\npattern add k --lengthBeats 4\n"
                      "pattern fill k 36 1\nclip add_pattern kick k 1.1.1 --repeat 2\n")
    r = cli("-p", p, "run", str(script))
    assert r.returncode == 0, r.stderr
    script.write_text("track add A\nmix set nope --gain 1\ntrack add B\n")
    r = cli("-p", p, "run", str(script))
    assert r.returncode == 1 and "line 2" in r.stderr


def rpc_lines(proc, reqs):
    out = []
    for q in reqs:
        proc.stdin.write(json.dumps(q) + "\n"); proc.stdin.flush()
        if "id" in q:
            out.append(json.loads(proc.stdout.readline()))
    return out


def test_jsonrpc_stdio(small_song):
    proc = subprocess.Popen([sys.executable, "-m", "plaindaw", "-p", str(small_song.dir), "rpc"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=ENV)
    try:
        r = rpc_lines(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "project.summary"},
            {"jsonrpc": "2.0", "id": 2, "method": "mix.set", "params": {"track": "bass", "gainDb": -5, "_as": "ai"}},
            {"jsonrpc": "2.0", "method": "mix.set", "params": {"track": "bass", "pan": 0.5}},  # notification
            {"jsonrpc": "2.0", "id": 3, "method": "no.such"},
            {"jsonrpc": "2.0", "id": 4, "method": "mix.set", "params": {"track": "bass", "gainDb": 100}},
            {"jsonrpc": "2.0", "id": 5, "method": "history.list", "params": {"limit": 2}},
            {"jsonrpc": "2.0", "id": 6, "method": "rpc.discover"},
        ])
        assert r[0]["result"]["title"] == "Test"
        assert r[1]["result"]["ok"]
        assert r[2]["error"]["code"] == -32601
        assert r[3]["error"]["code"] == -32602
        hist = r[4]["result"]
        assert hist[0]["author"] == "ai" and hist[1]["op"] == "mix.set"
        assert len(r[5]["result"]["ops"]) >= 50
        proc.stdin.write("not json\n"); proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["error"]["code"] == -32700
        proc.stdin.write(json.dumps([{"jsonrpc": "2.0", "id": 7, "method": "project.summary"},
                                     {"jsonrpc": "2.0", "id": 8, "method": "track.list"}]) + "\n"); proc.stdin.flush()
        assert [x["id"] for x in json.loads(proc.stdout.readline())] == [7, 8]
    finally:
        proc.stdin.close(); proc.wait(10)


def test_mcp_with_official_client(small_song):
    mcp = pytest.importorskip("mcp")
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "plaindaw", "-p", str(small_song.dir), "mcp"],
                                       env={"PYTHONPATH": str(ROOT), "PATH": os.environ["PATH"]})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                assert init.server_info.name == "plaindaw"
                names = {t.name for t in (await s.list_tools()).tools}
                assert {"edit", "batch", "render", "analyze", "undo", "project_overview", "list_plugins"} <= names
                res = await s.call_tool("edit", {"op": "mix.set", "args": {"track": "bass", "gainDb": -4}})
                assert not res.is_error and '"ok": true' in res.content[0].text
                res = await s.call_tool("edit", {"op": "mix.set", "args": {"track": "zzz", "gainDb": -4}})
                assert res.is_error
                res = await s.call_tool("analyze", {"target": "streaming"})
                assert "integratedLufs" in res.content[0].text
                res = await s.call_tool("history", {"limit": 1})
                assert '"author": "ai"' in res.content[0].text
    anyio.run(go)


@pytest.fixture
def server(small_song):
    from plaindaw.rpc import make_server
    srv = make_server(small_song, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield small_song, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(url, body):
    req = urllib.request.Request(url + "/rpc", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def test_http_rpc_and_sse(server):
    s, url = server
    assert "PlainDAW" in urllib.request.urlopen(url + "/", timeout=10).read().decode()
    events = []

    def listen():
        with urllib.request.urlopen(url + "/events", timeout=10) as r:
            for line in r:
                if line.startswith(b"data:"):
                    events.append(json.loads(line[5:]))
                    if len(events) >= 2:
                        return
    th = threading.Thread(target=listen, daemon=True); th.start()
    time.sleep(0.3)
    r = post(url, {"jsonrpc": "2.0", "id": 1, "method": "mix.set", "params": {"track": "drums", "gainDb": -2}})
    assert r["result"]["ok"]
    th.join(5)
    assert events[0]["type"] == "hello" and events[1]["type"] == "op"
    r = post(url, {"jsonrpc": "2.0", "id": 2, "method": "render", "params": {"formats": "mp3", "name": "t"}})
    assert urllib.request.urlopen(url + "/renders/t.mp3", timeout=10).status == 200
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url + "/renders/../project.json", timeout=10)
