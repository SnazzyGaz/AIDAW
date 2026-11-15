"""Write audio files: WAV (16/24/32f), FLAC, AIFF natively; MP3/OGG/OPUS/M4A via ffmpeg."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

NATIVE = {"wav": "WAV", "flac": "FLAC", "aiff": "AIFF", "aif": "AIFF"}
FFMPEG = {"mp3": ["-c:a", "libmp3lame"], "ogg": ["-c:a", "libvorbis"], "opus": ["-c:a", "libopus"],
          "m4a": ["-c:a", "aac"]}
SUBTYPES = {"16": "PCM_16", "24": "PCM_24", "32": "PCM_32", "32f": "FLOAT", "float": "FLOAT"}


class ExportError(RuntimeError):
    pass


def tpdf_dither(x: np.ndarray, bits: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    lsb = 1.0 / (2 ** (bits - 1))
    return x + (rng.random(x.shape) - rng.random(x.shape)) * lsb


def write_audio(path: Path, x: np.ndarray, sr: int, *, bits: str = "24", bitrate: str = "320k",
                dither: bool = True, normalize_peak_db: float | None = None, tags: dict | None = None) -> dict:
    """x is (2, n) float. Format from the extension."""
    path = Path(path)
    fmt = path.suffix.lower().lstrip(".")
    path.parent.mkdir(parents=True, exist_ok=True)
    y = np.asarray(x, dtype=np.float64)
    if normalize_peak_db is not None:
        pk = float(np.max(np.abs(y))) or 1.0
        y = y * (10 ** (normalize_peak_db / 20) / pk)
    bits = str(bits)
    if fmt in NATIVE:
        sub = SUBTYPES.get(bits)
        if sub is None:
            raise ExportError(f"bits must be 16, 24, 32 or 32f (got {bits})")
        if fmt == "flac" and sub in ("FLOAT", "PCM_32"):
            sub = "PCM_24"
        if sub == "PCM_16" and dither:
            y = tpdf_dither(y, 16)
        if sub != "FLOAT":
            y = np.clip(y, -1.0, 1.0 - 1.0 / 2 ** 23)
        sf.write(path, y.T, sr, format=NATIVE[fmt], subtype=sub)
        return {"path": str(path), "format": fmt, "bits": bits, "sampleRate": sr, "seconds": round(y.shape[1] / sr, 3)}
    if fmt in FFMPEG:
        from . import ffmpeg_exe
        exe = ffmpeg_exe()
        if not exe:
            raise ExportError(f"exporting .{fmt} needs ffmpeg on PATH or PLAINDAW_FFMPEG (WAV/FLAC work without it)")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "in.wav"
            sf.write(tmp, np.clip(y, -1, 1).T, sr, subtype="FLOAT")
            cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(tmp), *FFMPEG[fmt]]
            if fmt != "flac":
                cmd += ["-b:a", bitrate]
            for k, v in (tags or {}).items():
                if v:
                    cmd += ["-metadata", f"{k}={v}"]
            if fmt == "opus" and sr not in (48000,):
                cmd += ["-ar", "48000"]
            cmd.append(str(path))
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise ExportError(f"ffmpeg failed: {r.stderr.strip()[-400:]}")
        return {"path": str(path), "format": fmt, "bitrate": bitrate, "sampleRate": sr, "seconds": round(y.shape[1] / sr, 3)}
    raise ExportError(f"unknown export format .{fmt} (wav, flac, aiff, mp3, ogg, opus, m4a)")
