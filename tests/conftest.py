import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
THIRD = Path(os.environ.get("PLAINDAW_THIRDPARTY", "/workspace/plaindaw-thirdparty"))
REAL_KIT = Path(os.environ.get("PLAINDAW_REAL_KIT", ROOT / "kit"))
# Tests never touch the user's kit or library: an empty kit (so compose uses the deterministic
# built-ins) and a scratch library index. Kit tests opt in with the `real_kit` fixture.
import tempfile as _tf
_scratch = Path(_tf.mkdtemp(prefix="pd-test-"))
os.environ["PLAINDAW_KIT"] = str(_scratch / "kit")
os.environ["PLAINDAW_LIBRARY"] = str(_scratch / "library.json")


@pytest.fixture
def real_kit(monkeypatch):
    if not (REAL_KIT / "kit.json").exists():
        pytest.skip("kit not installed (plaindaw kit install)")
    monkeypatch.setenv("PLAINDAW_KIT", str(REAL_KIT))
    from plaindaw.plugins import scan
    scan.reset_cache()
    yield REAL_KIT
    scan.reset_cache()


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
