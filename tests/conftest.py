import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
THIRD = Path(os.environ.get("PLAINDAW_THIRDPARTY", "/workspace/plaindaw-thirdparty"))


@pytest.fixture
def proj(tmp_path):
    from plaindaw.session import Session, create_project
    p = create_project(tmp_path / "song", "Test", 120)
    return Session(p)


@pytest.fixture
def small_song(proj):
    s = proj
    s.apply("track.add", {"name": "Drums", "plugin": "builtin:drums"})
    s.apply("pattern.add", {"id": "beat", "lengthBeats": 4})
    s.apply("pattern.fill", {"pattern": "beat", "key": "36", "every": 1})
    s.apply("clip.add_pattern", {"track": "drums", "pattern": "beat", "at": "1.1.1", "repeat": 2})
    s.apply("track.add", {"name": "Bass", "plugin": "builtin:synth", "params": {"cutoff": 800}})
    s.apply("clip.add_notes", {"track": "bass", "at": "1.1.1", "notes": [{"t": 0.5, "len": 0.4, "key": "F2"}, {"t": 1.5, "len": 0.4, "key": "F2"}]})
    return s


surge = pytest.mark.skipif(not (THIRD / "Surge XT.clap").exists(), reason="Surge XT not installed")
