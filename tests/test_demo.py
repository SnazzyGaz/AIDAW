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


AI_SCRIPT = SCRIPT.parent / "build_demo_ai.pdaw"


def test_ai_demo_builds_with_reasons(tmp_path, capsys):
    """The AI demo (compose/arrange/fix/variations/humanize) with the deterministic built-ins."""
    p = str(tmp_path / "demo-ai")
    assert main(["new", p, "--bpm", "126", "--title", "AIDAW AI Demo"]) == 0
    assert main(["-p", p, "run", str(AI_SCRIPT)]) == 0
    from plaindaw.session import Session
    s = Session(p)
    ops = [e["op"] for e in s.history(50)]
    assert ops[:2] == ["compose.song", "arrange"] and "fix.clash" in ops and "humanize" in ops
    assert all(e.get("reason") for e in s.history(50))
    rep = s.analyze()
    sec = {x["name"]: x["integratedLufs"] for x in rep["sections"]}
    assert sec["Drop"] > sec["Build"] + 3 and sec["Drop"] > sec["Intro"]
    assert rep["master"]["clippedSamples"] == 0
