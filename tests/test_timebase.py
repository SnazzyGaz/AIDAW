import pytest

from plaindaw.timebase import TimeMap, TimeError, note_name, note_number


def tm(**kw):
    return TimeMap({"tempo": kw.get("tempo", [{"at": "1.1.1", "bpm": 120}]),
                    "timeSig": kw.get("sig", [{"at": "1.1.1", "num": 4, "den": 4}])})


def test_positions_44():
    t = tm()
    assert t.qn("1.1.1") == 0
    assert t.qn("2.1.1") == 4
    assert t.qn("1.2.1") == 1
    assert t.qn("1.1.2") == 0.25
    assert t.qn("1.1.1.120") == pytest.approx(0.125)
    assert t.qn(6.5) == 6.5
    for q in (0, 0.25, 3.75, 17, 33.5):
        assert t.qn(t.pos(q)) == pytest.approx(q)


def test_time_signature_changes():
    t = tm(sig=[{"at": "1.1.1", "num": 4, "den": 4}, {"at": "3.1.1", "num": 7, "den": 8}])
    assert t.qn("3.1.1") == 8
    assert t.qn("4.1.1") == 8 + 3.5
    assert t.pos(8 + 3.5) == "4.1.1"
    with pytest.raises(TimeError):
        tm(sig=[{"at": "2.3.1", "num": 3, "den": 4}])
    with pytest.raises(TimeError):
        t.qn("3.8.1")  # 7/8 has 7 beats


def test_tempo_map_seconds():
    t = tm(tempo=[{"at": "1.1.1", "bpm": 120}, {"at": "3.1.1", "bpm": 60}])
    assert t.sec(8) == pytest.approx(4.0)
    assert t.sec(9) == pytest.approx(5.0)
    assert t.qn_at_sec(5.0) == pytest.approx(9)
    assert t.bpm_at(7.9) == 120 and t.bpm_at(8) == 60


def test_bad_positions():
    t = tm()
    for bad in ("0.1.1", "1.0.1", "x", "-1", True):
        with pytest.raises(TimeError):
            t.qn(bad)


def test_note_names():
    assert note_number("C4") == 60
    assert note_number("A4") == 69
    assert note_number("F#3") == 54 == note_number("Gb3")
    assert note_number(61) == 61
    assert note_name(60) == "C4"
    for k in range(128):
        assert note_number(note_name(k)) == k
