"""Find VST3 and CLAP plugins on disk and resolve project plugin references.

References in a project look like:
  "clap:<plugin id>"            e.g. "clap:org.surge-synth-team.surge-xt"
  "vst3:<plugin name>"          e.g. "vst3:Surge XT"
  either may carry a "path" hint in the insert/instrument object.
Search path: PLAINDAW_PLUGIN_PATH (os.pathsep separated) + platform defaults.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..native_build import test_plugin_path


def default_dirs() -> list[Path]:
    env = [Path(p) for p in os.environ.get("PLAINDAW_PLUGIN_PATH", "").split(os.pathsep) if p]
    home = Path.home()
    if sys.platform == "win32":
        cf = Path(os.environ.get("COMMONPROGRAMFILES", r"C:\Program Files\Common Files"))
        la = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        sys_dirs = [cf / "VST3", cf / "CLAP", la / "Programs/Common/VST3", la / "Programs/Common/CLAP"]
    elif sys.platform == "darwin":
        sys_dirs = [Path("/Library/Audio/Plug-Ins/VST3"), Path("/Library/Audio/Plug-Ins/CLAP"),
                    home / "Library/Audio/Plug-Ins/VST3", home / "Library/Audio/Plug-Ins/CLAP"]
    else:
        sys_dirs = [home / ".vst3", home / ".clap", Path("/usr/lib/vst3"), Path("/usr/local/lib/vst3"),
                    Path("/usr/lib/clap"), Path("/usr/local/lib/clap")]
    return env + sys_dirs


def _iter_bundles(d: Path):
    if not d.exists():
        return
    for root, dirs, files in os.walk(d):
        r = Path(root)
        for name in list(dirs) + files:
            if name.endswith(".vst3") or name.endswith(".clap"):
                yield r / name
        dirs[:] = [x for x in dirs if not (x.endswith(".vst3") or x.endswith(".clap"))]


def scan(dirs: list[Path] | None = None, probe: bool = True) -> list[dict]:
    """Return a list of plugin descriptors. CLAP bundles are probed (cheap); VST3
    names come from the bundle name (probing VST3 means loading it)."""
    from .clap import list_plugins
    found: list[dict] = []
    try:
        paths = [test_plugin_path()]
    except Exception:  # no C compiler: skip the bundled test plugin
        paths = []
    for d in dirs or default_dirs():
        paths.extend(_iter_bundles(d))
    seen = set()
    for p in paths:
        p = Path(p)
        if p in seen:
            continue
        seen.add(p)
        if p.suffix == ".clap":
            try:
                for desc in list_plugins(p) if probe else []:
                    found.append({"format": "clap", "ref": f"clap:{desc['id']}", "name": desc["name"],
                                  "vendor": desc["vendor"], "version": desc["version"],
                                  "instrument": desc["instrument"], "path": str(p)})
            except Exception as e:  # broken bundle
                found.append({"format": "clap", "ref": None, "name": p.stem, "path": str(p), "error": str(e)})
        elif p.suffix == ".vst3":
            found.append({"format": "vst3", "ref": f"vst3:{p.stem}", "name": p.stem, "path": str(p)})
    return found


_cache: dict | None = None


def resolve(ref: str, path_hint: str | None = None) -> tuple[str, str, str | None]:
    """Return (format, filesystem path, plugin id or None) for a reference."""
    global _cache
    fmt, _, ident = ref.partition(":")
    if fmt not in ("clap", "vst3"):
        raise ValueError(f"not an external plugin ref: {ref}")
    if path_hint and Path(path_hint).exists():
        return fmt, path_hint, ident if fmt == "clap" else None
    if _cache is None:
        _cache = {d["ref"]: d for d in scan() if d.get("ref")}
    d = _cache.get(ref)
    if d is None:  # case-insensitive name match for vst3
        for k, v in _cache.items():
            if k.lower() == ref.lower() or (v.get("name", "").lower() == ident.lower() and v["format"] == fmt):
                d = v
                break
    if d is None:
        raise FileNotFoundError(f"plugin not found: {ref} (set PLAINDAW_PLUGIN_PATH or give a path)")
    return fmt, d["path"], ident if fmt == "clap" else None


def reset_cache() -> None:
    global _cache
    _cache = None


if __name__ == "__main__":
    print(json.dumps(scan(), indent=2))
