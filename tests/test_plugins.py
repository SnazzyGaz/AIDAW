import numpy as np
import pytest

from conftest import THIRD, surge
from plaindaw.native_build import test_plugin_path as plugin_path
from plaindaw.plugins.clap import ClapPlugin, list_plugins
from plaindaw.plugins.sandbox import PluginCrashed, run_job


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


def test_clap_effect_param_changes_gain():
    p = str(plugin_path())
    x = np.ones((2, 1000)) * 0.25
    job = {"format": "clap", "path": p, "plugin_id": "org.plaindaw.testgain", "sample_rate": 48000, "block": 256,
           "n_frames": 1000, "audio": x, "params": {"Gain": -6.0206}}
    out = run_job(job, sandbox=False)["audio"]
    assert np.allclose(out[:, 10:], 0.125, atol=1e-4)


def test_sandbox_survives_crash():
    p = str(plugin_path())
    job = {"format": "clap", "path": p, "plugin_id": "org.plaindaw.testgain", "sample_rate": 48000, "block": 256,
           "n_frames": 1000, "audio": np.zeros((2, 1000)), "params": {"Crash": 1.0}}
    with pytest.raises(PluginCrashed):
        run_job(job, sandbox=True)


def test_crashing_insert_in_project_is_bypassed(small_song):
    s = small_song
    clean = s.render(stems=True).stems["bass"]
    s.apply("plugin.add", {"track": "bass", "plugin": "clap:org.plaindaw.testgain", "params": {"Crash": 1}})
    r = s.render(stems=True)
    assert any("crash" in w for w in r.warnings)
    assert np.allclose(r.stems["bass"], clean)


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
