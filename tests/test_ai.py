"""Listening module, musical commands, sound library, SFZ player and kit plumbing."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from plaindaw import listen as L
from plaindaw import theory as th

SR = 48000


def clicks(bpm=120.0, n=32, jitter=0.0, seed=0, sr=SR):
    y = np.zeros(int(sr * (n * 60 / bpm + 1)))
    rng = np.random.default_rng(seed)
    for i in range(n):
        t = int((i * 60 / bpm + rng.normal(0, jitter) + 0.1) * sr)
        y[t:t + 400] += np.hanning(400) * np.sign(rng.normal(size=400))
    return np.vstack([y, y])


def tone(freqs, sec=2.0, sr=SR):
    t = np.arange(int(sec * sr)) / sr
    y = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs) * 0.5
    return np.vstack([y, y])


# ------------------------------------------------------------------ theory
def test_theory_keys_and_progressions():
    assert th.parse_key("F minor") == (5, "minor")
    assert th.parse_key("Fm") == (5, "minor")
    ch = th.parse_progression("i VI III VII", 5, "minor")
    assert [th.chord_symbol(r, q) for r, _, q in ch] == ["Fm", "Db", "Ab", "Eb"]
    vo = th.voice_lead(ch, center=60, voices=4)
    assert all(len(v) == 4 for v in vo)
    assert max(abs(a - b) for a, b in zip(vo[0], vo[1])) <= 2      # smooth voice leading
    assert [n for n, _ in th.parse_form("intro:8,drop:16")] == ["intro", "drop"]


# ------------------------------------------------------------------ listening
def test_listen_tempo_and_timing():
    tight = L.listen(clicks(120), SR)
    assert abs(tight["tempo"]["bpm"] - 120) < 0.6
    assert tight["timing"]["feel"] == "tight" and tight["timing"]["medianDevMs"] < 3
    loose = L.listen(clicks(120, jitter=0.02, seed=3), SR)
    assert loose["timing"]["medianDevMs"] > tight["timing"]["medianDevMs"] + 3


def test_listen_key_chords_and_timbre():
    # C major triad then F major then G major then C, 1 s each at 60 BPM bars of 1 s
    seq = np.hstack([tone(f, 1.0) for f in ([261.6, 329.6, 392.0], [349.2, 440.0, 523.3], [392.0, 493.9, 587.3],
                                             [261.6, 329.6, 392.0])])
    rep = L.listen(seq, SR, bpm_hint=240, bar_sec=1.0)
    assert rep["key"]["key"] == "C major"
    assert rep["chordsPerBar"][:4] == ["C", "F", "G", "C"]
    noise = np.random.default_rng(1).normal(size=(2, SR * 2)) * 0.2
    tags = L.timbre(noise, SR)["tags"]
    assert "bright" in tags or "noisy" in tags
    sub = tone([45.0], 2.0)
    assert "subby" in L.timbre(sub, SR)["tags"]


def test_reference_comparison_plain_english():
    a = clicks(120) + tone([110, 220], 17.0)[:, : clicks(120).shape[1]]
    b = a * 0.3
    cmp = L.compare_reference(a, b, SR, SR)
    assert any("louder" in d for d in cmp["differences"])
    assert cmp["mix"]["lufs"] > cmp["reference"]["lufs"]


def test_listen_plugin_hook(monkeypatch):
    monkeypatch.setenv("PLAINDAW_LISTENERS", "listener_example:features,nope.missing:x")
    rep = L.listen(clicks(120, n=8), SR)
    assert rep["extra"]["listener_example:features"]["peak"] > 0
    assert "error" in rep["extra"]["nope.missing:x"]


# ------------------------------------------------------------------ compose / arrange
def notes_of(s, track):
    t = next(x for x in s.doc["tracks"] if x["id"] == track)
    return [s.doc["patterns"][c["pattern"]]["notes"] for c in t["clips"]]


def test_compose_is_one_undo_step_with_reason(proj):
    s = proj
    r = s.call("compose", {"part": "bass", "key": "F minor", "chords": "i VI III VII", "bars": 4, "seed": 1})
    assert r["ok"] and r["key"] == "F minor" and r["chords"] == ["Fm", "Db", "Ab", "Eb"]
    h = s.history()
    assert len(h) == 1 and h[0]["op"] == "compose.bass" and "offbeat" in h[0]["reason"]
    roots = sorted({n["key"] % 12 for n in notes_of(s, "bass")[0]})
    assert roots == sorted({5, 1, 8, 3})
    s.undo()
    assert not [t for t in s.doc["tracks"] if t["type"] == "instrument"]


def test_compose_deterministic_and_seeded(tmp_path):
    from plaindaw.session import Session, create_project
    out = []
    for i, seed in enumerate((7, 7, 8)):
        s = Session(create_project(tmp_path / f"p{i}", "T", 128))
        s.call("compose", {"part": "lead", "key": "A minor", "bars": 8, "seed": seed, "style": "anthem"})
        out.append(json.dumps(notes_of(s, "lead")))
    assert out[0] == out[1] and out[0] != out[2]


def test_lead_fits_scale_and_chords(proj):
    s = proj
    s.call("compose", {"part": "lead", "key": "F minor", "bars": 8, "seed": 4})
    pcs = set(th.scale_pcs(5, "minor"))
    ns = notes_of(s, "lead")[0]
    assert ns and all(n["key"] % 12 in pcs for n in ns)
    assert ns[-1]["key"] % 12 == 5            # phrase ends on the tonic


def test_compose_song_and_arrange_render_deterministic(proj):
    s = proj
    r = s.call("compose", {"part": "song", "genre": "house", "seed": 2,
                           "form": "intro:4,groove:8,break:4,drop:8,outro:4"})
    assert r["arrange"]["bars"] == 28
    assert [m["name"] for m in s.doc["markers"]] == ["Intro", "Groove", "Break", "Drop", "Outro"]
    assert s.doc["transport"]["tempo"][0]["bpm"] == 124
    roles = {t.get("role") for t in s.doc["tracks"]}
    assert {"kick", "bass", "chords", "lead", "pad", "hat", "crash"} <= roles
    hist = [e["op"] for e in s.history()]
    assert hist == ["compose.song", "arrange"]
    a = s.render("5.1.1", "7.1.1")
    b = s.render("5.1.1", "7.1.1")
    assert np.abs(a.mix).max() > 0.01
    assert hashlib.sha256(a.mix.tobytes()).hexdigest() == hashlib.sha256(b.mix.tobytes()).hexdigest()


def test_arrange_needs_parts(proj):
    from plaindaw.ops import OpError
    with pytest.raises(OpError):
        proj.call("arrange", {"form": "intro:4,drop:8"})


def test_locks_apply_to_musical_commands(proj):
    from plaindaw.ops import OpError
    s = proj
    s.call("compose", {"part": "bass", "bars": 2})
    s.apply("lock.set", {"target": "bass"})
    with pytest.raises(OpError):
        s.call("compose", {"part": "bass", "bars": 2, "seed": 9}, author="ai")


# ------------------------------------------------------------------ fix clash / variations / humanize / preview
def test_fix_clash_reports_before_after(proj):
    s = proj
    s.call("compose", {"part": "drums", "genre": "bigroom", "bars": 2})
    s.apply("track.add", {"name": "Sub", "plugin": "builtin:synth", "role": "bass",
                          "params": {"wave": "sine", "attack": 0.002, "sustain": 1.0, "gainDb": -3}})
    s.apply("clip.add_notes", {"track": "sub", "at": "1.1.1", "notes": [{"t": 0, "len": 8, "key": "F1"}]})
    r = s.call("fix.clash", {"a": "kick", "b": "sub", "start": "1.1.1", "end": "3.1.1"})
    assert r["applied"] and r["after"]["index"] < r["before"]["index"]
    assert "clash index" in s.history()[-1]["reason"]
    sub = next(t for t in s.doc["tracks"] if t["id"] == "sub")
    assert any(i.get("sidechain") == "kick" for i in sub["inserts"])


def test_variations_list_and_keep(proj):
    s = proj
    s.call("compose", {"part": "drums", "bars": 2})
    r = s.call("variations", {"n": 3, "command": "compose bass --bars 2", "target": "loudness", "keep": "list"})
    assert len(r["results"]) == 3 and not r["applied"]
    r2 = s.call("variations", {"n": 3, "command": "compose bass --bars 2", "target": "loudness"})
    assert r2["applied"] and r2["best"] == r["best"]          # deterministic choice
    assert s.history()[-1]["reason"].startswith("variations:")
    r3 = s.call("variations", {"n": 3, "command": "mix set kick --gain -6", "vary": "gainDb=-12..0",
                               "target": "loudness", "keep": "list"})
    assert r3["best"] == "gainDb=0"


def test_humanize_and_swing(proj):
    s = proj
    s.call("compose", {"part": "drums", "bars": 2, "genre": "techno"})
    before = json.dumps(s.doc["patterns"])
    r = s.call("humanize", {"track": "hats", "feel": "loose", "seed": 3})
    assert r["afterMs"][r["patterns"][0]] > r["beforeMs"][r["patterns"][0]]
    assert s.history()[-1]["op"] == "humanize"
    s.undo()
    assert json.dumps(s.doc["patterns"]) == before
    s.apply("pattern.add", {"id": "sw", "lengthBeats": 1, "notes": [{"t": 0, "len": 0.2, "key": 42}, {"t": 0.25, "len": 0.2, "key": 42}]})
    s.apply("pattern.swing", {"pattern": "sw", "amount": 0.33})
    assert [n["t"] for n in s.doc["patterns"]["sw"]["notes"]] == [0.0, 0.3325]


def test_preview_bars_and_solo(proj):
    s = proj
    s.call("compose", {"part": "song", "genre": "lofi", "seed": 1, "form": "intro:2,a:4"})
    r = s.call("preview", {"bars": "3-4", "tracks": ["kick"], "listen": False})
    x, sr = sf.read(r["path"])
    assert r["start"] == "3.1.1" and 4.0 < len(x) / sr < 9.0
    r2 = s.call("preview", {"bars": "A", "listen": True})
    assert r2["listen"]["bpm"]


# ------------------------------------------------------------------ SFZ player + library
def make_sfz(d: Path) -> Path:
    t = np.arange(SR) / SR
    sf.write(str(d / "c4.wav"), 0.5 * np.sin(2 * np.pi * 261.63 * t), SR)
    (d / "inst.sfz").write_text("<control> default_path=./\n<global> ampeg_release=0.1\n"
                                "<region> sample=c4.wav pitch_keycenter=60 lokey=0 hikey=127\n")
    return d / "inst.sfz"


def test_builtin_sfz_player_pitches(proj, tmp_path):
    s = proj
    make_sfz(s.dir)
    s.apply("track.add", {"name": "Keys", "plugin": "sfz:inst.sfz"})
    s.apply("clip.add_notes", {"track": "keys", "at": "1.1.1", "notes": [{"t": 0, "len": 1, "key": 72}]})
    res = s.render("1.1.1", "2.1.1", stems=True)
    y = res.stems["keys"][0][: SR // 2]
    f = np.fft.rfftfreq(len(y), 1 / res.sr)[np.argmax(np.abs(np.fft.rfft(y)))]
    assert abs(f - 523.25) < 5


def test_library_scan_tag_search(tmp_path):
    from plaindaw import library
    d = tmp_path / "Samples"
    (d / "Drums").mkdir(parents=True)
    t = np.arange(int(SR * 0.4)) / SR
    kick = np.sin(2 * np.pi * (50 + 120 * np.exp(-t * 30)) * t) * np.exp(-t * 8)
    sf.write(str(d / "Drums" / "Big Kick 01.wav"), kick, SR)
    sf.write(str(d / "Drums" / "thing.wav"), kick, SR)                         # no name hint
    sf.write(str(d / "Groove Loop 120bpm.wav"), clicks(120, n=16)[0], SR)
    sf.write(str(d / "Warm Pad Cm.wav"), tone([261.6, 311.1, 392.0], 4.0)[0], SR)
    third = Path("/workspace/plaindaw-thirdparty/surge-patches/Basses/Bass 1.fxp")
    if third.exists():
        (d / "Presets").mkdir()
        (d / "Presets" / "Bass 1.fxp").write_bytes(third.read_bytes())
    (d / "My.vstpreset").write_bytes(b"VST3" + b"\x01\x00\x00\x00" + b"ABCDEF019182FAEB566D624153675854" + b"\0" * 40)
    make_sfz(d)
    r = library.scan([d])
    assert r["total"] >= 6 and r["errors"] == 0
    assert library.scan([d])["unchanged"] == r["total"]                        # incremental
    hits = library.search("kick")
    assert hits and hits[0]["name"] == "Big Kick 01"
    auto = next(e for e in library.load()["items"].values() if e["name"] == "thing")
    assert auto["soundType"] == "kick"
    loop = library.search("loop")[0]
    assert loop["bpm"] == 120
    assert library.search("pad", key="C minor")[0]["name"] == "Warm Pad Cm"
    vp = library.search("", kind="preset", engine="vst3")[0]
    assert library.get(vp["id"])["classId"] == "ABCDEF019182FAEB566D624153675854"
    if third.exists():
        b = library.search("bass", engine="surge")[0]
        assert library.get(b["id"]).get("category")
    a = library.audition(hits[0]["id"], out=str(tmp_path / "a.wav"))
    assert Path(a["path"]).exists()
    sfz_item = library.search("", kind="instrument")[0]
    a2 = library.audition(sfz_item["id"], out=str(tmp_path / "b.wav"))
    assert a2["seconds"] > 2


def test_lib_use_puts_item_on_track(proj, tmp_path):
    from plaindaw import library
    d = tmp_path / "lib"
    d.mkdir()
    make_sfz(d)
    sf.write(str(d / "Clap 1.wav"), np.random.default_rng(0).normal(size=4800) * 0.3, SR)
    library.scan([d])
    clap = library.search("clap")[0]
    r = proj.call("lib.use", {"id": clap["id"], "track": "Clap"})
    assert r["ok"] and proj.history()[-1]["op"] == "lib.use"
    inst = library.search("", kind="instrument")[0]
    proj.call("lib.use", {"id": inst["id"]})
    assert any(t.get("instrument", {}).get("plugin", "").startswith("sfz:") for t in proj.doc["tracks"])


# ------------------------------------------------------------------ kit
def test_kit_not_installed_falls_back(proj):
    from plaindaw import kit
    from plaindaw.ops import OpError
    assert kit.installed() == {"surge": False, "sfizz": False, "sfz": []}
    r = proj.call("compose", {"part": "bass", "bars": 1})
    assert r["sounds"]["bass"].startswith("built-in")
    with pytest.raises(OpError):
        proj.call("instrument.add", {"plugin": "surge"})


def test_kit_sfz_mapping_and_pins():
    from plaindaw import kit
    pins = kit._pins()
    assert len(pins["surge_patches"]) >= len(set(kit.SURGE_PATCHES.values()))
    assert all(f in pins["vsco"] for st in kit.SFZ_SETS.values() for f in st["files"])
    files = [Path(f"Player_dyn{d}_rr1_{i:03d}.wav") for d in (1, 3) for i in range(0, 45, 4)]
    text = kit.write_sfz("piano", files)
    assert text.count("<region>") == 24 and "lovel=73" in text
    s = kit.write_sfz("strings", [Path(f"VlnEns_susVib_{n}_v2.wav") for n in ("G2", "A2", "F#3")])
    assert "pitch_keycenter=54" in s
    assert kit._git_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_kit_instruments_render(proj, real_kit):
    s = proj
    s.call("instrument.add", {"plugin": "surge", "preset": "Bass 1", "name": "SurgeBass"})
    s.call("instrument.add", {"plugin": "sfizz", "sfz": "piano", "name": "Piano"})
    s.apply("clip.add_notes", {"track": "surgebass", "at": "1.1.1", "notes": [{"t": 0, "len": 1, "key": "F2"}]})
    s.apply("clip.add_notes", {"track": "piano", "at": "1.1.1", "notes": [{"t": 0, "len": 1, "key": "C4"}]})
    res = s.render("1.1.1", "2.1.1", stems=True)
    assert np.abs(res.stems["surgebass"]).max() > 0.01
    assert np.abs(res.stems["piano"]).max() > 0.01
    r = s.call("compose", {"part": "pad", "bars": 1})
    assert r["sounds"]["pad"].startswith("Surge XT")


# ------------------------------------------------------------------ front ends
def test_cli_musical_commands(tmp_path):
    import os, subprocess, sys
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}

    def cli(*a):
        r = subprocess.run([sys.executable, "-m", "plaindaw", "-p", str(tmp_path / "s"), *a], capture_output=True,
                           text=True, env=env, timeout=180)
        assert r.returncode == 0, r.stderr
        return r.stdout
    subprocess.run([sys.executable, "-m", "plaindaw", "new", str(tmp_path / "s")], env=env, check=True, capture_output=True)
    out = cli("compose", "song", "--genre", "techno", "--seed", "1", "--form", "intro:4,drop:8")
    assert "compose.song" in out and "why:" in out
    assert "clash" in cli("clash", "report", "--bars", "5-6").lower()
    assert "tempo" in cli("listen", "--start", "5.1.1", "--end", "7.1.1").lower()
    hist = cli("history")
    assert "why:" in hist and "arrange" in hist
    j = json.loads(cli("lib", "search", "kick", "--json"))
    assert isinstance(j, list)


def test_mcp_and_rpc_expose_ai_tools(proj):
    from plaindaw.mcp_server import Server
    from plaindaw import rpc
    srv = Server(proj)
    names = {t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert {"listen", "compose", "arrange", "fix_clash", "variations", "humanize", "preview", "lib_search", "kit_status"} <= names
    r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "compose", "arguments": {"part": "bass", "bars": 2, "seed": 1}}})
    assert not r["result"]["isError"], r
    assert proj.history()[-1]["author"] == "ai"
    out = rpc.handle(proj, {"jsonrpc": "2.0", "id": 3, "method": "cli.exec", "params": {"line": "compose drums --bars 2"}})
    assert out["result"]["method"] == "compose" and "compose.drums" in out["result"]["text"]
    out = rpc.handle(proj, {"jsonrpc": "2.0", "id": 4, "method": "listen", "params": {}})
    assert out["result"]["tempo"]["bpm"]
    bad = rpc.handle(proj, {"jsonrpc": "2.0", "id": 5, "method": "cli.exec", "params": {"line": "serve"}})
    assert "error" in bad


def test_recompose_arranged_part_swaps_every_clip(proj):
    s = proj
    s.call("compose", {"part": "song", "genre": "house", "seed": 2, "form": "intro:4,drop:8,break:4,drop:8"})
    lead = next(t for t in s.doc["tracks"] if t.get("role") == "lead")
    ats = [c["at"] for c in lead["clips"]]
    r = s.call("variations", {"n": 2, "command": ["compose", "lead", "--style", "pluck"], "target": "keyfit"})
    assert r["applied"] and r["range"][0] == s.doc["transport"]["loop"]["start"]
    lead = next(t for t in s.doc["tracks"] if t.get("role") == "lead")
    assert [c["at"] for c in lead["clips"]] == ats
    assert len({c["pattern"] for c in lead["clips"]}) == 1 and "pluck" in lead["clips"][0]["pattern"]


def test_variations_forwards_flags_to_the_command(proj):
    from plaindaw.cli import build_call
    m, params, _ = build_call(["variations", "2", "compose", "lead", "--style", "pluck", "--bars", "2", "--target", "keyfit",
                               "--keep", "list"])
    r = proj.call(m, params)
    assert len(r["results"]) == 2
    r2 = proj.call(m, {**params, "keep": "best"})
    lead = next(t for t in proj.doc["tracks"] if t.get("role") == "lead")
    pat = proj.doc["patterns"][lead["clips"][0]["pattern"]]
    assert "pluck" in lead["clips"][0]["pattern"] and pat["lengthBeats"] == 8
