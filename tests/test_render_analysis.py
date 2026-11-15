import hashlib

import numpy as np
import pyloudnorm as pyln
import pytest

from plaindaw.analysis import analyze, compare, loudness
from plaindaw.render import render


def h(x):
    return hashlib.sha256(np.ascontiguousarray(x, dtype=np.float64).tobytes()).hexdigest()


def test_render_is_deterministic(small_song):
    s = small_song
    s.apply("track.add", {"name": "Verb", "type": "return"})
    s.apply("plugin.add", {"track": "verb", "plugin": "builtin:reverb"})
    s.apply("send.add", {"track": "bass", "to": "verb", "levelDb": -6})
    s.apply("plugin.add", {"track": "bass", "plugin": "builtin:chorus"})
    a = s.render()
    b = s.render()
    assert a.mix.shape == b.mix.shape and h(a.mix) == h(b.mix)
    assert np.max(np.abs(a.mix)) > 0.01


def test_render_length_and_tail(small_song):
    r = small_song.render()
    # 2 bars at 120 bpm = 4 s + 2 s tail
    assert r.duration == pytest.approx(6.0, abs=0.01)
    r2 = small_song.render(start="2.1.1", end="3.1.1")
    assert r2.start_sec == pytest.approx(2.0)


def test_mute_solo_sends(small_song):
    s = small_song
    full = s.render(stems=True)
    s.apply("mix.set", {"track": "drums", "solo": True})
    solo = s.render(stems=True)
    assert np.max(np.abs(solo.stems["bass"])) == 0
    assert np.allclose(solo.stems["drums"], full.stems["drums"])
    s.apply("mix.set", {"track": "drums", "solo": False, "mute": True})
    muted = s.render(stems=True)
    assert np.max(np.abs(muted.stems["drums"])) == 0
    assert np.allclose(muted.stems["bass"], full.stems["bass"])


def test_gain_and_pan_math(small_song):
    s = small_song
    s.apply("mix.set", {"track": "drums", "mute": True})
    a = s.render(stems=True).stems["bass"]
    s.apply("mix.set", {"track": "bass", "gainDb": -6.0206})
    b = s.render(stems=True).stems["bass"]
    assert np.allclose(b, a * 0.5, atol=1e-9)
    s.apply("mix.set", {"track": "bass", "gainDb": 0, "pan": -1})
    c = s.render(stems=True).stems["bass"]
    assert np.max(np.abs(c[1])) < 1e-12 and np.max(np.abs(c[0])) > 0


def test_automation_changes_level(small_song):
    s = small_song
    s.apply("mix.set", {"track": "drums", "mute": True})
    s.apply("auto.set", {"track": "bass", "target": "mix/gainDb", "points": [["1.1.1", -60], ["2.1.1", -60], ["2.1.2", 0]]})
    r = s.render(stems=True).stems["bass"]
    sr = r.shape[1] and 48000
    assert np.max(np.abs(r[:, : int(1.9 * sr)])) < 0.01


def test_sidechain_ducks(small_song):
    s = small_song
    s.apply("track.add", {"name": "Pad", "plugin": "builtin:synth"})
    s.apply("clip.add_notes", {"track": "pad", "at": "1.1.1", "notes": [{"t": 0, "len": 8, "key": "C4"}]})
    plain = s.render(stems=True).stems["pad"]
    s.apply("plugin.add", {"track": "pad", "plugin": "builtin:compressor", "sidechain": "drums",
                           "params": {"thresholdDb": -40, "ratio": 10}})
    ducked = s.render(stems=True).stems["pad"]
    sr = 48000
    beat = slice(int(1.0 * sr), int(1.05 * sr))       # right after a kick
    between = slice(int(1.35 * sr), int(1.45 * sr))   # between kicks
    duck_ratio = np.sqrt(np.mean(ducked[:, beat] ** 2)) / np.sqrt(np.mean(plain[:, beat] ** 2))
    rest_ratio = np.sqrt(np.mean(ducked[:, between] ** 2)) / np.sqrt(np.mean(plain[:, between] ** 2))
    assert duck_ratio < rest_ratio


def test_audio_clip_timing_and_stretch(proj, tmp_path):
    import soundfile as sf
    sr = 48000
    click = np.zeros(sr); click[0] = 0.9
    f = tmp_path / "click.wav"; sf.write(f, click, sr, subtype="FLOAT")
    s = proj
    s.apply("track.add", {"name": "A", "type": "audio"})
    s.apply("clip.add", {"track": "a", "file": str(f), "at": "2.1.1"})
    r = s.render(stems=True).stems["a"]
    assert int(np.argmax(np.abs(r[0]))) == 2 * sr  # bar 2 at 120 bpm = 2.0 s
    # tempo-stretch a 1 s file recorded at 60 bpm into a 120 bpm song -> 0.5 s
    tone = 0.3 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)
    g = tmp_path / "tone.wav"; sf.write(g, tone, sr)
    s.apply("track.add", {"name": "B", "type": "audio"})
    s.apply("clip.add", {"track": "b", "file": str(g), "at": "1.1.1", "stretch": {"mode": "tempo", "sourceBpm": 60}})
    rb = s.render(stems=True).stems["b"]
    nz = np.where(np.abs(rb[0]) > 0.01)[0]
    assert (nz[-1] - nz[0]) / sr == pytest.approx(0.5, abs=0.03)


def test_loudness_matches_reference():
    sr = 48000
    rng = np.random.default_rng(1)
    t = np.arange(sr * 8) / sr
    for x in (np.vstack([0.1 * np.sin(2 * np.pi * 1000 * t)] * 2), rng.normal(0, 0.05, (2, sr * 8)),
              np.vstack([0.2 * np.sin(2 * np.pi * 100 * t), 0.05 * rng.normal(size=t.size)])):
        ours = loudness(x, sr)["integratedLufs"]
        ref = pyln.Meter(sr).integrated_loudness(x.T)
        assert ours == pytest.approx(ref, abs=0.1)


def test_ebu_reference_sine():
    # EBU Tech 3341: 1 kHz stereo sine at -23 dBFS reads -23.0 LUFS (+-0.1)
    sr = 48000
    t = np.arange(sr * 20) / sr
    a = 10 ** (-23 / 20)
    x = np.vstack([a * np.sin(2 * np.pi * 1000 * t)] * 2)
    assert loudness(x, sr)["integratedLufs"] == pytest.approx(-23.0, abs=0.1)


def test_analysis_fields_and_warnings():
    sr = 48000
    t = np.arange(sr * 4) / sr
    x = np.vstack([np.sin(2 * np.pi * 50 * t), -np.sin(2 * np.pi * 50 * t)])  # full scale, out of phase
    a = analyze(x, sr, target="streaming")
    m = a["master"]
    assert m["stereoCorrelation"] < -0.99
    assert m["truePeakDbtp"] >= -0.1
    assert any("phase" in w for w in a["warnings"])
    assert any("true peak" in w for w in a["warnings"])
    assert a["target"]["pass"] is False
    assert max(a["bandsDbRel"], key=a["bandsDbRel"].get).startswith("sub")
    silent = analyze(np.zeros((2, sr)), sr)
    assert silent["master"]["silent"] is True


def test_compare_identical_and_gain():
    sr = 48000
    x = np.random.default_rng(2).normal(0, 0.1, (2, sr * 4))
    c = compare(x, x, sr)
    assert c["identical"] and c["deltaLu"] == 0
    c2 = compare(x, x * 0.5, sr)
    assert c2["deltaLu"] == pytest.approx(-6.02, abs=0.05)
    assert c2["nullResidualLufs"] is None or c2["nullResidualLufs"] < -60


def test_session_analysis_sections_and_tracks(small_song):
    s = small_song
    s.apply("marker.add", {"name": "Intro", "at": "1.1.1"})
    s.apply("marker.add", {"name": "Drop", "at": "2.1.1"})
    rep = s.analyze(target="club")
    assert [x["name"] for x in rep["sections"]] == ["Intro", "Drop"]
    assert set(rep["tracks"]) == {"drums", "bass"}
    assert rep["target"]["profile"] == "club"
