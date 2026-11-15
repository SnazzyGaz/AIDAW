"""Find VST3 and CLAP plugins on disk and resolve project plugin references.

References in a project look like:
  "clap:<plugin id>"            e.g. "clap:org.surge-synth-team.surge-xt"
  "vst3:<plugin name>"          e.g. "vst3:Surge XT"
  either may carry a "path" hint in the insert/instrument object.
Search path: PLAINDAW_PLUGIN_PATH (os.pathsep separated) + platform defaults.

Scanning is metadata-only by default on Windows: nothing is instantiated, because a plugin's
loader can pop up modal dialogs (licence errors, activation, updates) on the user's desktop.
PLAINDAW_PLUGIN_PROBE=1 turns probing on, =0 turns it off everywhere.
PLAINDAW_PLUGIN_SKIP is a ';'-separated list of glob patterns matched against bundle names
(default: Waves shells and anything under a Waves folder); skipped plugins are never listed,
probed or loaded. Set it to an empty string to skip nothing.
"""
from __future__ import annotations

import fnmatch
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
    try:
        from ..kit import plugin_dirs
        kit_dirs = plugin_dirs()
    except Exception:
        kit_dirs = []
    return env + kit_dirs + sys_dirs


DEFAULT_SKIP = ("WaveShell*", "*Waves*")


def skip_patterns() -> list[str]:
    raw = os.environ.get("PLAINDAW_PLUGIN_SKIP")
    if raw is None:
        return list(DEFAULT_SKIP)
    return [x.strip() for x in raw.split(";") if x.strip()]


def is_skipped(path) -> bool:
    """True when any part of the plugin's path matches a skip pattern (case-insensitive)."""
    pats = [x.lower() for x in skip_patterns()]
    if not pats:
        return False
    parts = [x.lower() for x in Path(path).parts]
    return any(fnmatch.fnmatchcase(part, pat) for part in parts for pat in pats)


def probe_default() -> bool:
    v = os.environ.get("PLAINDAW_PLUGIN_PROBE")
    if v is not None and v.strip() != "":
        return v.strip().lower() not in ("0", "false", "no", "off")
    return sys.platform != "win32"


def vst3_moduleinfo_classes(path) -> dict:
    """Class ids -> names from a VST3 bundle's Contents/Resources/moduleinfo.json (VST3 SDK 3.7+),
    read without loading the plugin. Empty when the bundle has no moduleinfo."""
    mi = Path(path) / "Contents" / "Resources" / "moduleinfo.json"
    try:
        txt = mi.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    import re
    out = {}
    # moduleinfo.json is JSON5-ish (comments, trailing commas): pull CID/Name pairs with a regex
    for m in re.finditer(r'"CID"\s*:\s*"([0-9A-Fa-f]{32})"(.*?)(?="CID"|$)', txt, re.S):
        nm = re.search(r'"Name"\s*:\s*"([^"]*)"', m.group(2))
        out[m.group(1).upper()] = nm.group(1) if nm else ""
    return out


def _iter_bundles(d: Path):
    if not d.exists():
        return
    for root, dirs, files in os.walk(d):
        r = Path(root)
        if is_skipped(r):
            dirs[:] = []
            continue
        for name in list(dirs) + files:
            if (name.endswith(".vst3") or name.endswith(".clap")) and not is_skipped(r / name):
                yield r / name
        dirs[:] = [x for x in dirs if not (x.endswith(".vst3") or x.endswith(".clap"))]


def scan(dirs: list[Path] | None = None, probe: bool | None = None) -> list[dict]:
    """Return a list of plugin descriptors. VST3 names come from the bundle name (and its
    moduleinfo.json when present), never from loading it. CLAP bundles are probed only when
    probing is on (probe_default(): off on Windows) and otherwise listed by file name."""
    from .clap import list_plugins
    if probe is None:
        probe = probe_default()
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
        if p.suffix == ".clap" and not probe:
            found.append({"format": "clap", "ref": None, "name": p.stem, "path": str(p), "probed": False})
        elif p.suffix == ".clap":
            try:
                for desc in list_plugins(p) if probe else []:
                    found.append({"format": "clap", "ref": f"clap:{desc['id']}", "name": desc["name"],
                                  "vendor": desc["vendor"], "version": desc["version"],
                                  "instrument": desc["instrument"], "path": str(p)})
            except Exception as e:  # broken bundle
                found.append({"format": "clap", "ref": None, "name": p.stem, "path": str(p), "error": str(e)})
        elif p.suffix == ".vst3":
            d = {"format": "vst3", "ref": f"vst3:{p.stem}", "name": p.stem, "path": str(p)}
            classes = vst3_moduleinfo_classes(p) if p.is_dir() else {}
            if classes:
                d["classes"] = classes
            found.append(d)
    return found


_cache: dict | None = None


def resolve(ref: str, path_hint: str | None = None) -> tuple[str, str, str | None]:
    """Return (format, filesystem path, plugin id or None) for a reference."""
    global _cache
    fmt, _, ident = ref.partition(":")
    if fmt not in ("clap", "vst3"):
        raise ValueError(f"not an external plugin ref: {ref}")
    if path_hint and is_skipped(path_hint):
        raise PermissionError(f"plugin is on the skip list (PLAINDAW_PLUGIN_SKIP): {path_hint}")
    if path_hint and Path(path_hint).exists():
        return fmt, path_hint, ident if fmt == "clap" else None
    try:
        from ..kit import SURGE_ID, resolve_ref
        from ..native_build import clap_host_available
        if fmt == "clap" and ident == SURGE_ID and not clap_host_available():
            vp = resolve_ref("vst3:Surge XT")      # no CLAP host here (no C compiler): same patch on the VST3
            if vp:
                return "vst3", vp, None
        kp = resolve_ref(ref)
    except Exception:
        kp = None
    if kp:
        return fmt, kp, ident if fmt == "clap" else None
    if _cache is None:
        _cache = {d["ref"]: d for d in scan() if d.get("ref")}
    d = _cache.get(ref)
    if d is None:  # case-insensitive name match for vst3
        for k, v in _cache.items():
            if k.lower() == ref.lower() or (v.get("name", "").lower() == ident.lower() and v["format"] == fmt):
                d = v
                break
    if d is None and fmt == "clap" and not probe_default():
        # an explicit CLAP id: probing was off, so look once with probing (skip list still applies)
        _cache.update({x["ref"]: x for x in scan(probe=True) if x.get("ref")})
        d = _cache.get(ref)
    if d is None:
        raise FileNotFoundError(f"plugin not found: {ref} (set PLAINDAW_PLUGIN_PATH or give a path)")
    return fmt, d["path"], ident if fmt == "clap" else None


def reset_cache() -> None:
    global _cache
    _cache = None


if __name__ == "__main__":
    print(json.dumps(scan(), indent=2))
