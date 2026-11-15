"""Industrial / liturgical sound tools: foundry kit, bell, synth LFO/FM/formant/drift, bitcrush, formant, saturator."""
import numpy as np
import pytest

from plaindaw import dsp

SR = 48000


def rng(i=0):
    return np.random.default_rng(i)


def p(name, **kw):
    d = {k: v for k, v in dsp.PARAMS[name].items() if not k.startswith("_")}
    d.update(kw)
    return d


@pytest.mark.parametrize("key", range(36, 47))
def test_foundry_hits_sound_and_are_bounded(key):
    y = dsp.foundry_hit(key, 120, SR, rng(key), p("foundry"))
    assert y.shape[0] == 2 and y.shape[1] > SR * 0.03
    assert np.isfinite(y).all() and 0.02 < np.abs(y).max() <= 1.0
    assert dsp.foundry_hit(99, 120, SR, rng(), p("foundry")).shape[1] == 1     # unmapped key: silence


def test_foundry_kick_is_sub_heavy_and_anvil_is_bright():
    k = dsp.foundry_hit(36, 120, SR, rng(), p("foundry"))[0]
    a = dsp.foundry_hit(38, 120, SR, rng(), p("foundry"))[0]
    def centroid(x):
        X = np.abs(np.fft.rfft(x)) ** 2; f = np.fft.rfftfreq(len(x), 1 / SR)    # energy-weighted
        return (X * f).sum() / X.sum()
    assert centroid(k) < 400 < centroid(a)


def test_bell_partials_are_inharmonic_and_long():
    y = dsp.bell_note(p("bell", decay=4.0), 60, 110, SR, rng())
    assert y.shape[1] > 4 * SR and np.abs(y).max() <= 1.0
    X = np.abs(np.fft.rfft(y[0, : 2 * SR])); f = np.fft.rfftfreq(2 * SR, 1 / SR)
    hum = dsp.mtof(60) / 2
    # hum (octave below the nominal) and a minor-third tierce partial are both present
    def band(fc): return X[(f > fc * 0.98) & (f < fc * 1.02)].max()
    assert band(hum) > 0.1 * X.max() and band(hum * 2 * 1.183) > 0.05 * X.max()


def test_synth_lfo_fm_noise_drift_formant():
    base = p("synth", wave="saw", attack=0.01, release=0.05, cutoff=4000)
    plain = dsp.synth_note(base, 45, 100, SR, SR, rng())
    wob = dsp.synth_note(dict(base, lfoRate=2.0, lfoCutoff=2.0, lfoAmp=0.8, lfoPitch=0.5), 45, 100, SR, SR, rng())
    assert not np.allclose(plain, wob)
    # amplitude LFO: RMS swings across the note
    rms = [np.sqrt(np.mean(wob[0, i:i + 2400] ** 2)) for i in range(4800, 43200, 2400)]
    assert max(rms) > 2 * min(rms)
    fm = dsp.synth_note(dict(base, wave="sine", fmRatio=1.4, fmIndex=2.0, fmDecay=0.3), 60, 100, SR // 2, SR, rng())
    assert np.isfinite(fm).all() and np.abs(fm).max() > 0.05
    voc = dsp.synth_note(dict(base, formant="a", noise=0.2, drift=15), 45, 100, SR, SR, rng())
    X = np.abs(np.fft.rfft(voc[0])); f = np.fft.rfftfreq(voc.shape[1], 1 / SR)
    assert X[(f > 600) & (f < 900)].mean() > 2 * X[(f > 4000) & (f < 6000)].mean()   # energy around F1 of 'a'


def test_bitcrush_formant_saturator_effects():
    t = np.arange(SR) / SR
    x = np.vstack([np.sin(2 * np.pi * 220 * t)] * 2) * 0.5
    ctx = dsp.Ctx(SR, lambda s: np.full(len(s), 120.0), SR)
    y = dsp.FX["bitcrush"](x, p("bitcrush", bits=3, downsample=8), ctx)
    assert len(np.unique(np.round(y[0], 6))) <= 2 ** 3 + 1
    assert np.allclose(y[0, :8], y[0, 0])                       # sample-and-hold
    sat = dsp.FX["saturator"](x, p("saturator", driveDb=18), ctx)
    assert np.abs(sat).max() < 1.0 and not np.allclose(sat, x)
    n = np.random.default_rng(0).uniform(-1, 1, (2, SR)) * 0.3
    v = dsp.FX["formant"](n, p("formant", vowel="u"), ctx)
    X = np.abs(np.fft.rfft(v[0])); f = np.fft.rfftfreq(SR, 1 / SR)
    assert X[(f > 250) & (f < 350)].mean() > 3 * X[(f > 5000) & (f < 7000)].mean()


def test_project_uses_new_tools(proj):
    s = proj
    s.apply("track.add", {"name": "Machines", "plugin": "builtin:foundry", "params": {"drive": 8}})
    s.apply("track.add", {"name": "Bell", "plugin": "builtin:bell", "params": {"decay": 3}})
    s.apply("clip.add_notes", {"track": "machines", "at": "1.1.1", "notes": [{"t": 0, "len": 0.2, "key": 36}, {"t": 1, "len": 0.2, "key": 38}]})
    s.apply("clip.add_notes", {"track": "bell", "at": "1.1.1", "notes": [{"t": 0, "len": 2, "key": 50}]})
    s.apply("plugin.add", {"track": "machines", "plugin": "builtin:bitcrush", "params": {"bits": 6}})
    s.apply("plugin.add", {"track": "bell", "plugin": "builtin:formant", "params": {"vowel": "o", "mix": 0.3}})
    s.apply("plugin.add", {"track": "bell", "plugin": "builtin:saturator"})
    from plaindaw.render import render
    res = render(s.doc, s.dir, sr=SR, end=8.0)
    assert np.abs(res.mix).max() > 0.05 and np.isfinite(res.mix).all()
    from plaindaw.session import _q_plugin_scan
    import plaindaw.session as ss
    ss._scan = lambda p: []                       # don't walk the system plugin folders here
    b = _q_plugin_scan(s, {}, "human")["builtin"]
    assert "builtin:foundry" in b["instruments"] and "builtin:bitcrush" in b["effects"]


def test_vst_preset_params_apply_first():
    from plaindaw.plugins.host import _ordered
    got = _ordered({"macro_dread": 0.8, "program": "Rust Works", "hall_size": 0.9})
    assert got[0] == ("program", "Rust Works")
    assert [k for k, _ in got[1:]] == ["macro_dread", "hall_size"]


def test_listen_bare_tracks_flag(proj):
    from plaindaw.session import _q_listen
    s = proj
    s.call("track.add", {"name": "Bell", "id": "bell", "plugin": "builtin:bell"})
    calls = []
    s.listen = lambda **kw: calls.append(kw) or {}
    _q_listen(s, {"tracks": True}, None)
    _q_listen(s, {"tracks": "Bell"}, None)
    assert calls[0]["tracks"] is None and calls[1]["tracks"] == ["bell"]
