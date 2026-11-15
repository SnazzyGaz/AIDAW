"""Builds the small C parts (CLAP host + test plugins) on first use if missing.

Needs a C compiler (cc/gcc/clang). On Windows use MSVC or MinGW via the same sources.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE_SRC = HERE / "native"
OUT = HERE / "_native"

LIB_NAME = "pdclap.dll" if sys.platform == "win32" else ("libpdclap.dylib" if sys.platform == "darwin" else "libpdclap.so")
TEST_PLUGIN = "plaindaw-test.clap"
DSP_NAME = LIB_NAME.replace("pdclap", "pddsp")


def _cc() -> str:
    for c in (os.environ.get("CC"), "cc", "gcc", "clang", "cl"):
        if c and shutil.which(c):
            return c
    raise RuntimeError("No C compiler found (set CC; on Windows install MinGW-w64 gcc or run from a "
                       "Visual Studio Developer prompt). Needed only for CLAP hosting; VST3 works without it.")


def clap_host_available() -> bool:
    """True if the CLAP host library is built or can be built (a C compiler is present)."""
    if (OUT / LIB_NAME).exists():
        return True
    try:
        _cc()
        return True
    except RuntimeError:
        return False


def _build(src: Path, out: Path, libs: list[str]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    inc = NATIVE_SRC / "third_party" / "clap-include"
    cc = _cc()
    if Path(cc).stem.lower() == "cl":  # MSVC
        cmd = [cc, "/nologo", "/O2", "/LD", f"/I{inc}", str(src), f"/Fe:{out}", f"/Fo:{out.parent}\\"]
        subprocess.run(cmd, check=True, capture_output=True)
        return
    if sys.platform == "win32":
        libs = [x for x in libs if x not in ("-ldl", "-lm")]
    cmd = [cc, "-O2", "-fPIC", "-shared", f"-I{inc}", "-o", str(out), str(src), *libs]
    if sys.platform == "darwin":
        cmd.insert(1, "-undefined")
        cmd.insert(2, "dynamic_lookup")
    subprocess.run(cmd, check=True, capture_output=True)


def _stale(out: Path, src: Path) -> bool:
    return not out.exists() or (src.exists() and src.stat().st_mtime > out.stat().st_mtime)


def ensure_built() -> dict[str, Path]:
    host_src = NATIVE_SRC / "src" / "pdclap.c"
    test_src = NATIVE_SRC / "src" / "testsynth.c"
    host = OUT / LIB_NAME
    test = OUT / TEST_PLUGIN
    if _stale(host, host_src):
        _build(host_src, host, ["-ldl"] if sys.platform.startswith("linux") else [])
    if _stale(test, test_src):
        _build(test_src, test, ["-lm"])
    return {"host": host, "test_plugin": test}


def dsp_lib() -> Path | None:
    """Optional fast DSP kernels; None if no compiler is available."""
    src = NATIVE_SRC / "src" / "pddsp.c"
    out = OUT / DSP_NAME
    try:
        if _stale(out, src):
            _build(src, out, ["-lm"])
        return out
    except Exception:
        return out if out.exists() else None


def test_plugin_path() -> Path:
    return ensure_built()["test_plugin"]


if __name__ == "__main__":
    print(ensure_built())
