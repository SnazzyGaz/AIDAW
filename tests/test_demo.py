"""The demo song builds from its command script and the drop is louder than the build."""
from pathlib import Path

from plaindaw.cli import main

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "demo" / "build_demo.pdaw"


def test_demo_builds_and_sounds_sane(tmp_path, capsys):
    p = str(tmp_path / "demo")
    assert main(["new", p, "--bpm", "126", "--title", "PlainDAW Demo"]) == 0
    assert main(["-p", p, "run", str(SCRIPT)]) == 0
    from plaindaw.session import Session
    s = Session(p)
    rep = s.analyze()
    sec = {x["name"]: x["integratedLufs"] for x in rep["sections"]}
    assert sec["Drop"] > sec["Build"] + 2
    assert rep["master"]["truePeakDbtp"] <= -0.9
    assert rep["master"]["clippedSamples"] == 0
    assert all(v.get("integratedLufs") for k, v in rep["tracks"].items())  # nothing silent
    assert len(s.history(500)) > 80
