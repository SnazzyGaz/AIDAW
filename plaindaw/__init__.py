"""PlainDAW: a small text-first DAW that people and AI agents drive through one command layer."""
__version__ = "0.2.0"
FORMAT = "plaindaw/0.1"


def ffmpeg_exe() -> str | None:
    """ffmpeg to use: $PLAINDAW_FFMPEG (a full path), else the one on PATH."""
    import os
    import shutil
    exe = os.environ.get("PLAINDAW_FFMPEG")
    if exe and os.path.isfile(exe):
        return exe
    return shutil.which("ffmpeg")


def data_home():
    """Per-user data folder (library index, auditions): $PLAINDAW_HOME, else ~/.plaindaw."""
    import os
    from pathlib import Path
    return Path(os.environ.get("PLAINDAW_HOME") or (Path.home() / ".plaindaw"))
