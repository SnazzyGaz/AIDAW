"""Media store: content-addressed audio under <project>/media, decoding and resampling."""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

NATIVE_EXT = {".wav", ".flac", ".aif", ".aiff", ".ogg"}


class MediaError(RuntimeError):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def import_file(project_dir: Path, src: Path) -> str:
    """Copy src into media/ named by hash. Returns the relative path 'media/<hash>.<ext>'.
    Formats libsndfile cannot read are converted to FLAC with ffmpeg."""
    src = Path(src)
    if not src.exists():
        raise MediaError(f"file not found: {src}")
    mdir = Path(project_dir) / "media"
    mdir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    if ext not in NATIVE_EXT:
        data = _ffmpeg_decode(src)
        tmp = mdir / ".import.flac"
        sf.write(tmp, data[0].T, data[1], format="FLAC", subtype="PCM_24")
        h = sha256_file(tmp)[:16]
        dst = mdir / f"{h}.flac"
        if dst.exists():
            tmp.unlink()
        else:
            tmp.rename(dst)
    else:
        h = sha256_file(src)[:16]
        dst = mdir / f"{h}{ext}"
        if not dst.exists():
            shutil.copyfile(src, dst)
    return f"media/{dst.name}"


def _ffmpeg_decode(path: Path) -> tuple[np.ndarray, int]:
    from . import ffmpeg_exe
    exe = ffmpeg_exe()
    if not exe:
        raise MediaError(f"ffmpeg needed to read {path.suffix} files (put it on PATH or set PLAINDAW_FFMPEG)")
    p = subprocess.run([exe, "-v", "error", "-i", str(path), "-f", "wav", "-acodec", "pcm_f32le", "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise MediaError(f"ffmpeg could not decode {path}: {p.stderr.decode(errors='replace')[-300:]}")
    data, sr = sf.read(io.BytesIO(p.stdout), always_2d=True, dtype="float64")
    return data.T, sr


def info(path: Path) -> dict:
    try:
        i = sf.info(str(path))
        return {"sampleRate": i.samplerate, "channels": i.channels, "durationSec": round(i.duration, 6),
                "format": i.format, "subtype": i.subtype}
    except Exception:
        d, sr = _ffmpeg_decode(Path(path))
        return {"sampleRate": sr, "channels": d.shape[0], "durationSec": round(d.shape[1] / sr, 6)}


@lru_cache(maxsize=64)
def _decode_cached(path: str, mtime: float, sr: int) -> np.ndarray:
    try:
        data, fsr = sf.read(path, always_2d=True, dtype="float64")
        data = data.T
    except Exception:
        data, fsr = _ffmpeg_decode(Path(path))
    if data.shape[0] == 1:
        data = np.vstack([data, data])
    elif data.shape[0] > 2:
        data = data[:2]
    if fsr != sr:
        fr = Fraction(sr, fsr).limit_denominator(1000)
        data = signal.resample_poly(data, fr.numerator, fr.denominator, axis=1)
    data.setflags(write=False)
    return data


def decode(path: Path, sr: int) -> np.ndarray:
    """(2, n) float64 at sr (read-only array; copy before modifying)."""
    path = Path(path)
    if not path.exists():
        raise MediaError(f"missing media file: {path}")
    return _decode_cached(str(path.resolve()), path.stat().st_mtime, int(sr))


def used_media(doc: dict) -> set[str]:
    out = set()
    for t in doc.get("tracks", []):
        for c in t.get("clips", []):
            if c.get("file"):
                out.add(c["file"])
        inst = t.get("instrument") or {}
        prm = inst.get("params", {})
        if prm.get("file"):
            out.add(prm["file"])
        for z in prm.get("zones", []) or []:
            if z.get("file"):
                out.add(z["file"])
    return out
