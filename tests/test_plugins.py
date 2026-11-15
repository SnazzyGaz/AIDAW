import numpy as np
import pytest

from conftest import THIRD, surge
from plaindaw.native_build import clap_host_available
from plaindaw.native_build import test_plugin_path as plugin_path
from plaindaw.plugins.clap import ClapPlugin, list_plugins
from plaindaw.plugins.sandbox import PluginCrashed, run_job

needs_cc = pytest.mark.skipif(not clap_host_available(), reason="no C compiler: CLAP host and test plugins can't be built")


@needs_cc
def test_clap_host_with_test_plugins():
    p = plugin_path()
    ids = {d["id"] for d in list_plugins(p)}
    assert {"org.plaindaw.testsynth", "org.plaindaw.testgain"} <= ids
    syn = ClapPlugin(p, "org.plaindaw.testsynth")
    try:
        syn.activate(48000, 256)
        out = syn.process(None, 4800, [(0, 0, 69, 0, 1.0), (2400, 1, 69, 0, 0.0)])
        assert out.shape == (2, 4800)
        assert np.max(np.abs(out[:, :2400])) > 0.05
        state = syn.get_state()
        assert isinstance(state, (bytes, bytearray))
    finally:
        syn.close()


@needs_cc
def test_clap_effect_param_changes_gain():
    p = str(plugin_path())
    x = np.ones((2, 1000)) * 0.25
    job = {"format": "clap", "path": p, "plugin_id": "org.plaindaw.testgain", "sample_rate": 48000, "block": 256,
           "n_frames": 1000, "audio": x, "params": {"Gain": -6.0206}}
    out = run_job(job, sandbox=False)["audio"]
    assert np.allclose(out[:, 10:], 0.125, atol=1e-4)


@needs_cc
def test_sandbox_survives_crash():
    p = str(plugin_path())
    job = {"format": "clap", "path": p, "plugin_id": "org.plaindaw.testgain", "sample_rate": 48000, "block": 256,
           "n_frames": 1000, "audio": np.zeros((2, 1000)), "params": {"Crash": 1.0}}
    with pytest.raises(PluginCrashed):
        run_job(job, sandbox=True)


@needs_cc
def test_crashing_insert_in_project_is_bypassed(small_song):
    s = small_song
    clean = s.render(stems=True).stems["bass"]
    s.apply("plugin.add", {"track": "bass", "plugin": "clap:org.plaindaw.testgain", "params": {"Crash": 1}})
    r = s.render(stems=True)
    assert any("crash" in w for w in r.warnings)
    assert np.allclose(r.stems["bass"], clean)


@needs_cc
def test_clap_instrument_track_renders(proj):
    s = proj
    s.apply("track.add", {"name": "T", "plugin": "clap:org.plaindaw.testsynth"})
    s.apply("clip.add_notes", {"track": "t", "at": "1.1.1", "notes": [{"t": 0, "len": 1, "key": "A4"}]})
    r = s.render(stems=True)
    assert np.max(np.abs(r.stems["t"])) > 0.05


@surge
def test_surge_clap_and_vst3(proj, monkeypatch):
    monkeypatch.setenv("PLAINDAW_PLUGIN_PATH", str(THIRD))
    from plaindaw.plugins.scan import reset_cache
    reset_cache()
    s = proj
    s.apply("track.add", {"name": "SC", "plugin": "clap:org.surge-synth-team.surge-xt"})
    s.apply("clip.add_notes", {"track": "sc", "at": "1.1.1", "notes": [{"t": 0, "len": 1, "key": "C4"}]})
    s.apply("track.add", {"name": "SV", "plugin": "vst3:Surge XT"})
    s.apply("clip.add_notes", {"track": "sv", "at": "1.1.1", "notes": [{"t": 0, "len": 1, "key": "C3"}]})
    s.apply("plugin.add", {"track": "sv", "plugin": "vst3:Surge XT Effects"})
    r = s.render(stems=True)
    assert not r.warnings, r.warnings
    assert np.max(np.abs(r.stems["sc"])) > 0.01
    assert np.max(np.abs(r.stems["sv"])) > 0.01
    params = s.call("plugin.params", {"plugin": "clap:org.surge-synth-team.surge-xt"})
    assert len(params["params"]) > 100
    reset_cache()


def test_scan_skip_list_and_metadata_only(tmp_path, monkeypatch):
    from plaindaw.plugins import scan as sc
    vst = tmp_path / "VST3"
    (vst / "WaveShell1-VST3 13.0_x64.vst3").mkdir(parents=True)
    (vst / "Waves" / "Some.vst3").mkdir(parents=True)
    good = vst / "Good Synth.vst3" / "Contents" / "Resources"
    good.mkdir(parents=True)
    (good / "moduleinfo.json").write_text('{"Classes": [{"CID": "0123456789ABCDEF0123456789ABCDEF", '
                                          '"Category": "Audio Module Class", "Name": "Good Synth",},],}')
    (vst / "Thing.clap").write_bytes(b"not a real clap")
    monkeypatch.delenv("PLAINDAW_PLUGIN_SKIP", raising=False)
    monkeypatch.setenv("PLAINDAW_PLUGIN_PROBE", "0")
    found = [d for d in sc.scan([vst]) if str(vst) in d["path"]]
    names = sorted(d["name"] for d in found)
    assert names == ["Good Synth", "Thing"]          # Waves skipped, the bogus CLAP listed without loading
    g = next(d for d in found if d["name"] == "Good Synth")
    assert g["classes"] == {"0123456789ABCDEF0123456789ABCDEF": "Good Synth"}
    assert next(d for d in found if d["name"] == "Thing")["probed"] is False
    with pytest.raises(PermissionError):
        sc.resolve("vst3:WaveShell1-VST3 13.0_x64", str(vst / "WaveShell1-VST3 13.0_x64.vst3"))
    monkeypatch.setenv("PLAINDAW_PLUGIN_SKIP", "")
    assert len([d for d in sc.scan([vst]) if str(vst) in d["path"]]) == 4
    monkeypatch.delenv("PLAINDAW_PLUGIN_PROBE")
    monkeypatch.setattr(sc.sys, "platform", "win32")
    assert sc.probe_default() is False
    monkeypatch.setattr(sc.sys, "platform", "linux")
    assert sc.probe_default() is True
