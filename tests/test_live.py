"""Live engine: render-ahead playback, incremental re-render, live notes, metronome, recording.

Uses the "manual" output (process() is called directly), so no audio device is needed."""
import numpy as np
import pytest

from plaindaw import live


@pytest.fixture
def eng(small_song):
    e = live.Engine(small_song, output="manual", blocksize=512)
    live.ENGINES[id(small_song)] = e
    assert e.wait_ready(60), e.error
    e.ensure_output()
    yield small_song, e
    e.close()
    live.ENGINES.pop(id(small_song), None)


def run(e, seconds):
    frames = int(seconds * e.sr)
    out = [e.process(512) for _ in range(frames // 512)]
    return np.concatenate(out) if out else np.zeros((0, 2), np.float32)


def test_playback_matches_timeline_and_loops(eng):
    s, e = eng
    assert e.buf is not None and e.render_info["renderedParts"] >= 1
    e.set_loop(True, "1.1.1", "2.1.1")
    assert e.wait_ready(60)
    e.play()
    y = run(e, 2.5)                       # one bar at 120 BPM is 2 s: we must have wrapped
    assert np.abs(y).max() > 0.05 and np.abs(y).max() <= 1.0
    st = e.status()
    assert st["playing"] and 0.0 <= st["posQn"] < 4.0 and st["loop"]
    e.stop(rewind=True)
    assert e.pos == e.ls and not e.playing


def test_edit_rerenders_incrementally(eng):
    s, e = eng
    s.apply("mix.set", {"track": "drums", "mute": True})
    assert e.wait_ready(60)
    info = e.render_info
    assert info["cachedParts"] >= 1 and info["renderedParts"] == 0
    s.apply("pattern.add_notes", {"pattern": "beat", "notes": [{"t": 0.5, "len": 0.1, "key": 38}]})
    assert e.wait_ready(60)
    assert e.render_info["renderedParts"] >= 1


def test_live_notes_and_metronome(eng):
    s, e = eng
    r = e.note_on("bass", "C3", 110)
    assert r["ok"] and r["track"] == "bass" and not r["standIn"]
    y = run(e, 0.2)
    assert np.abs(y).max() > 0.01
    assert e.status()["active"] == [{"track": "bass", "key": 48, "vel": 110}]
    e.note_off("bass", 48)
    run(e, 1.0)
    assert not e.voices and not e.status()["active"]
    # drums sound immediately too
    assert e.note_on("drums", 36)["ok"]
    assert np.abs(run(e, 0.1)).max() > 0.01
    # metronome: clicks on the beats even over silence
    s.apply("mix.set", {"track": "drums", "mute": True})
    s.apply("mix.set", {"track": "bass", "mute": True})
    assert e.wait_ready(60)
    run(e, 1.5)                                          # let the kick ring out
    assert not e.voices
    e.seek(0); e.set_metronome(True); e.play()
    y = run(e, 1.1)
    hits = np.nonzero(np.abs(y[:, 0]) > 0.05)[0]
    assert len(hits) and hits[0] < 0.05 * e.sr          # first click right at bar 1
    assert (np.abs(y[int(0.49 * e.sr):int(0.56 * e.sr), 0]) > 0.05).any()   # second beat at 0.5 s
    assert not (np.abs(y[int(0.1 * e.sr):int(0.45 * e.sr), 0]) > 0.05).any()   # silence between


def test_record_commits_one_quantized_step(eng):
    s, e = eng
    e.set_loop(False)
    assert e.wait_ready(60)
    e.set_record(True, track="bass", quantize=0.5)
    e.seek("1.2.1"); e.play()
    run(e, 0.05)
    e.note_on(None, 60, 90)
    run(e, 0.3)
    e.note_off(None, 60)
    n0 = len(s.history(100))
    e.stop()
    hist = s.history(100)
    assert len(hist) == n0 + 1 and "record" in str(hist[-1])
    notes = [n for p in s.doc["patterns"].values() for n in p["notes"] if n.get("key") == 60]
    assert notes and all(abs(n["t"] / 0.5 - round(n["t"] / 0.5)) < 1e-6 for n in notes)


def test_transport_queries_cli_and_mcp(eng):
    s, e = eng
    st = s.call("transport.status", {})
    assert st["output"] == "manual" and "latencyMs" in st
    assert s.call("transport.seek", {"at": "2.1.1"})["posQn"] == pytest.approx(4.0)
    assert s.call("transport.metronome", {"on": "on"})["metronome"] is True
    assert s.call("live.note", {"track": "bass", "key": "A2"})["key"] == 45
    devs = s.call("audio.devices", {})
    assert "devices" in devs
    import io
    from plaindaw.cli import run_line
    buf = io.StringIO()
    run_line(s, ["status"], "human", buf)
    assert "2.1.1" in buf.getvalue()
    from plaindaw import mcp_server
    names = [t[0] for t in mcp_server.TOOLS]
    assert "transport" in names and "play_note" in names
