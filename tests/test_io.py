from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from plaindaw.export import ExportError, write_audio
from plaindaw.midi import export_midi, read_midi


def test_export_formats(small_song, tmp_path):
    s = small_song
    r = s.export(out=str(tmp_path / "out"), formats="wav,flac,mp3,ogg", bits="16", stems=True, target="streaming")
    paths = {f["path"].rsplit(".", 1)[-1]: f["path"] for f in r["files"] if "stems" not in Path(f["path"]).parts}
    assert set(paths) == {"wav", "flac", "mp3", "ogg"}
    info = sf.info(paths["wav"])
    assert info.subtype == "PCM_16" and info.samplerate == 48000 and info.channels == 2
    assert sf.info(paths["flac"]).duration == pytest.approx(info.duration, abs=0.01)
    import subprocess, json
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name", "-of", "json", paths["mp3"]],
                        capture_output=True, text=True)
    meta = json.loads(pr.stdout)["format"]
    assert meta["format_name"] == "mp3" and float(meta["duration"]) == pytest.approx(info.duration, abs=0.1)
    assert any("stems" in Path(f["path"]).parts for f in r["files"])
    assert (tmp_path / "out" / "Test.analysis.json").exists()
    assert r["analysis"]["target"]["profile"] == "streaming"


def test_export_float_and_errors(tmp_path):
    x = np.vstack([np.linspace(-1.5, 1.5, 1000)] * 2)
    write_audio(tmp_path / "f.wav", x, 48000, bits="32f")
    y, _ = sf.read(tmp_path / "f.wav", always_2d=True)
    assert np.max(y) > 1.4  # float keeps overs
    with pytest.raises(ExportError):
        write_audio(tmp_path / "x.xyz", x, 48000)
    with pytest.raises(ExportError):
        write_audio(tmp_path / "x.wav", x, 48000, bits="12")


def test_midi_roundtrip(small_song, tmp_path):
    s = small_song
    s.apply("tempo.set", {"bpm": 128})
    s.apply("marker.add", {"name": "Drop", "at": "2.1.1"})
    out = tmp_path / "song.mid"
    r = export_midi(s.doc, out)
    assert r["notes"] == 8 + 2
    m = read_midi(out)
    assert m["tempo"][0][1] == pytest.approx(128, abs=0.01)
    assert (4.0, "Drop") in m["markers"]
    drums = next(t for t in m["tracks"] if t["name"] == "Drums")
    assert drums["channel"] == 9 and [n["t"] for n in drums["notes"]] == [0, 1, 2, 3, 4, 5, 6, 7]
    # import into a fresh project
    from plaindaw.session import Session, create_project
    s2 = Session(create_project(tmp_path / "p2"))
    res = s2.import_midi(out)
    assert {t["track"] for t in res["tracks"]} == {"drums", "bass"}
    assert s2.doc["transport"]["tempo"][0]["bpm"] == pytest.approx(128, abs=0.01)
    assert next(t for t in s2.doc["tracks"] if t["id"] == "drums")["instrument"]["plugin"] == "builtin:drums"


def test_import_stems(proj, tmp_path):
    sr = 48000
    for name in ("vox", "gtr"):
        sf.write(tmp_path / f"{name}.wav", 0.1 * np.random.default_rng(0).normal(size=sr), sr)
    r = proj.import_stems([str(tmp_path / "vox.wav"), str(tmp_path / "gtr.wav")])
    assert r["tracks"] == ["vox", "gtr"]
    assert proj.render().duration == pytest.approx(3.0, abs=0.01)


def test_musical_diff(small_song):
    s = small_song
    n = s.entries[-1]["n"]
    s.apply("mix.set", {"track": "bass", "gainDb": -3})
    s.apply("pattern.add_notes", {"pattern": "beat", "notes": [{"t": 0.5, "len": 0.25, "key": 42}]})
    d = s.call("diff", {"since": n})["changes"]
    assert any("Bass (bass): mix/gainDb 0.0 → -3.0" in c for c in d)
    assert any("pattern beat: + F#2" in c for c in d)
