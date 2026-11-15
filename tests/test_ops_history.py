import json

import pytest

from plaindaw.ops import REGISTRY, OpError, op_catalog
from plaindaw.session import NeedsConfirm, PermissionDenied, Session


def test_every_op_documented():
    cat = op_catalog()
    assert len(cat) == len(REGISTRY) >= 50
    for o in cat:
        assert o["summary"]
        for p in o["positional"]:
            assert p in o["args"]


def test_apply_undo_redo_persist(small_song):
    s = small_song
    before = json.dumps(s.doc, sort_keys=True)
    r = s.apply("mix.set", {"track": "bass", "gainDb": -6, "pan": 0.3})
    assert r["n"] and s.doc["tracks"][1]["mix"]["gainDb"] == -6
    s.undo()
    assert json.dumps(s.doc, sort_keys=True) == before
    s.redo()
    assert s.doc["tracks"][1]["mix"]["pan"] == 0.3
    # reload from disk: same doc and same undo stack
    s2 = Session(s.dir)
    assert s2.doc == s.doc
    assert s2.applied == s.applied
    s2.undo()
    assert json.dumps(s2.doc, sort_keys=True) == before


def test_new_edit_clears_redo(small_song):
    s = small_song
    s.apply("mix.set", {"track": "bass", "gainDb": -6})
    s.undo()
    s.apply("mix.set", {"track": "bass", "gainDb": -3})
    with pytest.raises(OpError):
        s.redo()


def test_undo_everything_returns_to_empty(small_song):
    s = small_song
    while s.applied:
        s.undo()
    assert [t["id"] for t in s.doc["tracks"]] == ["master"]
    assert s.doc["patterns"] == {}


def test_revert_specific_and_conflict(small_song):
    s = small_song
    a = s.apply("mix.set", {"track": "bass", "gainDb": -6})["n"]
    b = s.apply("mix.set", {"track": "drums", "gainDb": -2})["n"]
    s.revert(a)  # independent of b
    assert s.doc["tracks"][1]["mix"]["gainDb"] == 0.0
    assert s.doc["tracks"][0]["mix"]["gainDb"] == -2
    c = s.apply("mix.set", {"track": "drums", "gainDb": -4})["n"]
    with pytest.raises(OpError, match="later edits"):
        s.revert(b)
    s.undo()  # undo c, then b can be reverted
    s.revert(b)
    assert s.doc["tracks"][0]["mix"]["gainDb"] == 0.0


def test_invalid_args_rejected_without_change(small_song):
    s = small_song
    before = json.dumps(s.doc, sort_keys=True)
    n = len(s.entries)
    for name, args in [("mix.set", {"track": "bass", "gainDb": 99}), ("mix.set", {"track": "nope", "gainDb": 1}),
                       ("track.add", {"name": "X", "type": "weird"}), ("clip.add_pattern", {"track": "bass", "pattern": "zzz", "at": "1.1.1"}),
                       ("pattern.add_notes", {"pattern": "beat", "notes": [{"t": 0, "len": 1, "key": 200}]}),
                       ("plugin.add", {"track": "bass", "plugin": "builtin:nonsense"}),
                       ("plugin.add", {"track": "bass", "plugin": "builtin:eq", "params": {"bogus": 1}}),
                       ("track.route", {"track": "bass", "to": "drums"}), ("mix.set", {"track": "bass", "foo": 1}),
                       ("tempo.set", {"bpm": 5})]:
        with pytest.raises(OpError):
            s.apply(name, args)
    assert json.dumps(s.doc, sort_keys=True) == before and len(s.entries) == n


def test_routing_loop_rejected(proj):
    s = proj
    s.apply("track.add", {"name": "A", "type": "bus"})
    s.apply("track.add", {"name": "B", "type": "bus", "output": "a"})
    with pytest.raises(OpError):
        s.apply("track.route", {"track": "a", "to": "b"})
    with pytest.raises(OpError):
        s.apply("send.add", {"track": "a", "to": "b"})


def test_batch_is_one_undo_step_and_atomic(proj):
    s = proj
    s.apply("batch", {"ops": [{"op": "track.add", "args": {"name": "One"}}, {"op": "track.add", "args": {"name": "Two"}}]})
    assert len(s.doc["tracks"]) == 3
    with pytest.raises(OpError, match="batch item 1"):
        s.apply("batch", {"ops": [{"op": "track.add", "args": {"name": "Three"}}, {"op": "mix.set", "args": {"track": "zz", "gainDb": 0}}]})
    assert len(s.doc["tracks"]) == 3  # nothing from the failed batch
    s.undo()
    assert len(s.doc["tracks"]) == 1


def test_locks_and_permissions(small_song):
    s = small_song
    s.apply("lock.set", {"target": "bass"})
    with pytest.raises(PermissionDenied):
        s.apply("mix.set", {"track": "bass", "gainDb": -3}, author="ai")
    s.apply("mix.set", {"track": "bass", "gainDb": -3}, author="human")  # lock is against the AI only
    s.apply("mix.set", {"track": "drums", "gainDb": -3}, author="ai")
    with pytest.raises(PermissionDenied):
        s.apply("lock.clear", {"target": "bass"}, author="ai")
    with pytest.raises(PermissionDenied):
        s.apply("track.remove", {"track": "bass"}, author="ai")
    s.apply("lock.clear", {"target": "bass"})
    s.apply("mix.set", {"track": "bass", "gainDb": -1}, author="ai")
    s.apply("lock.set", {"target": "pattern:beat"})
    with pytest.raises(PermissionDenied):
        s.apply("pattern.transpose", {"pattern": "beat", "semitones": 1}, author="ai")
    ro = Session(s.dir, author="ai", mode="read-only")
    with pytest.raises(PermissionDenied):
        ro.apply("mix.set", {"track": "drums", "gainDb": 0})
    ask = Session(s.dir, author="ai", mode="ask")
    with pytest.raises(NeedsConfirm):
        ask.apply("track.remove", {"track": "drums"})
    ask.apply("track.remove", {"track": "drums"}, confirm=True)
    assert all(t["id"] != "drums" for t in ask.doc["tracks"])


def test_dry_run_changes_nothing(small_song):
    s = small_song
    n = len(s.entries)
    r = s.apply("track.remove", {"track": "bass"}, dry_run=True)
    assert r["dryRun"] and any("removed track" in c for c in r["changes"])
    assert len(s.entries) == n and any(t["id"] == "bass" for t in s.doc["tracks"])


def test_history_authors_and_external_edit(small_song):
    s = small_song
    s.apply("mix.set", {"track": "bass", "gainDb": -2}, author="ai")
    h = s.history(3)
    assert h[-1]["author"] == "ai" and h[-1]["op"] == "mix.set"
    # hand edit of project.json is picked up and recorded
    import time
    time.sleep(0.01)
    p = s.dir / "project.json"
    p.write_text(p.read_text().replace('"title": "Test"', '"title": "Edited"'))
    assert s.reload_if_changed()
    assert s.doc["meta"]["title"] == "Edited"
    assert s.history(1)[-1]["op"] == "file.edit"


def test_note_ops(proj):
    s = proj
    s.apply("pattern.add", {"id": "p", "lengthBeats": 4})
    s.apply("pattern.add_notes", {"pattern": "p", "notes": [{"t": 0.02, "len": 0.5, "key": "C4"}, {"t": 1.1, "len": 0.5, "key": "E4", "vel": 90}]})
    s.apply("pattern.quantize", {"pattern": "p", "grid": 0.25})
    assert [n["t"] for n in s.doc["patterns"]["p"]["notes"]] == [0.0, 1.0]
    s.apply("pattern.transpose", {"pattern": "p", "semitones": 12})
    assert [n["key"] for n in s.doc["patterns"]["p"]["notes"]] == [72, 76]
    s.apply("pattern.edit_notes", {"pattern": "p", "filter": {"key": 76}, "set": {"vel": 30}})
    assert s.doc["patterns"]["p"]["notes"][1]["vel"] == 30
    s.apply("pattern.remove_notes", {"pattern": "p", "indices": [0]})
    assert len(s.doc["patterns"]["p"]["notes"]) == 1
    s.apply("pattern.humanize", {"pattern": "p", "seed": 3})
    d1 = json.dumps(s.doc["patterns"]["p"])
    s.undo(); s.apply("pattern.humanize", {"pattern": "p", "seed": 3})
    assert json.dumps(s.doc["patterns"]["p"]) == d1  # seeded = repeatable


def test_clip_ops(small_song):
    s = small_song
    s.apply("clip.move", {"track": "drums", "clip": "drums_c1", "by": 4})
    assert s.doc["tracks"][0]["clips"][0]["at"] == "2.1.1"
    s.apply("clip.split", {"track": "drums", "clip": "drums_c1", "at": "3.1.1"})
    cl = s.doc["tracks"][0]["clips"]
    assert [c.get("repeat", 1) for c in cl] == [1, 1] and cl[1]["at"] == "3.1.1"
    s.apply("clip.duplicate", {"track": "drums", "clip": cl[1]["id"]})
    assert s.doc["tracks"][0]["clips"][-1]["at"] == "4.1.1"
    r = s.apply("clip.make_unique", {"track": "drums", "clip": "drums_c1"})
    assert r["pattern"] in s.doc["patterns"]


def test_automation_ops(small_song):
    s = small_song
    s.apply("auto.set", {"track": "bass", "target": "mix/gainDb", "points": [["1.1.1", -20], ["3.1.1", 0]]})
    s.apply("plugin.add", {"track": "bass", "plugin": "builtin:filter"})
    s.apply("auto.shape", {"track": "bass", "target": "inserts/bass_filter/cutoff", "start": "1.1.1", "end": "2.1.1",
                           "from": 200, "to": 4000, "shape": "exp", "steps": 8})
    lanes = {a["target"]: a for a in s.doc["tracks"][1]["automation"]}
    assert len(lanes["inserts/bass_filter/cutoff"]["points"]) == 9
    with pytest.raises(OpError):
        s.apply("auto.set", {"track": "bass", "target": "inserts/bass_filter/type", "points": [["1.1.1", 1]]})
    s.apply("auto.clear", {"track": "bass", "target": "mix/gainDb"})


def test_media_import_and_audio_clip(proj, tmp_path):
    import numpy as np
    import soundfile as sf
    f = tmp_path / "tone.wav"
    sf.write(f, 0.3 * np.sin(2 * np.pi * 440 * np.arange(24000) / 48000), 48000)
    s = proj
    s.apply("track.add", {"name": "Vox", "type": "audio"})
    r = s.apply("clip.add", {"track": "vox", "file": str(f), "at": "1.1.1", "fadeIn": 0.01})
    assert r["file"].startswith("media/") and (s.dir / r["file"]).exists()
    r2 = s.apply("clip.add", {"track": "vox", "file": str(f), "at": "2.1.1"})
    assert r2["file"] == r["file"]  # content-addressed, stored once
    assert len(list((s.dir / "media").glob("*.wav"))) == 1
