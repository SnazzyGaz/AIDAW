"""The default instrument kit: Surge XT (synth, GPL-3.0) + sfizz (SFZ sampler, BSD-2-Clause)
+ a small CC0 SFZ set (upright piano, violin section) from VS Chamber Orchestra 2 CE.

Nothing large lives in git. `plaindaw kit install` downloads the official release builds for
this OS into a local kit folder, verifies checksums pinned below, and writes kit.json so the
instruments resolve by name (`instrument add surge`, `instrument add sfizz --sfz piano`,
plugin refs `clap:org.surge-synth-team.surge-xt` and `sfz:piano`).

Kit folder: $PLAINDAW_KIT, else <PlainDAW folder>/kit (on Windows always under the app's own
folder, never Program Files), else ~/.local/share/plaindaw/kit.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

SURGE_VERSION = "1.3.4"
SURGE_TAG = "release_xt_1.3.4"
SURGE_ID = "org.surge-synth-team.surge-xt"
SFIZZ_VERSION = "1.2.3"
VSCO_COMMIT = "440300901dfe9275fd84e0b7763af1f8443ae62e"

SURGE_ASSETS = {  # official release builds (plugins only), md5 from the release's md5sum.txt
    "linux": {"file": "surge-xt-linux-1.3.4-pluginsonly.tar.gz", "md5": "0180f06ec7a8445b1c749471e29c702b",
              "sha256": "dd431b75f5fa197c4bffa6ca27ca46970f0a94c834119bb1db7decdeec4c28db"},
    "windows": {"file": "surge-xt-win64-1.3.4-pluginsonly.zip", "md5": "d318e95ca79a5ee70ddafec04313a00c",
                "sha256": "564e162c560af07ad4ed47fe1bfcd827cf97a575de30d06c48249aad2e7c35e6"},
    "macos": {"file": "surge-xt-macos-1.3.4-pluginsonly.zip", "md5": "8afca4159d9b417c5e07ebc1a5e96ed3", "sha256": None},
}
SURGE_URL = "https://github.com/surge-synthesizer/releases-xt/releases/download/1.3.4/"
SFIZZ_ASSETS = {  # official sfizz library releases: sfizz_render (headless SFZ renderer) + libsfizz
    "windows": {"file": "sfizz-1.2.3-win64.zip", "sha256": "7431cbe9b7f2e71a9ff4b12fbbe82c766dbf75b1eedecca627e7506cb06a9fcc"},
    "macos": {"file": "sfizz-1.2.3-macos.tar.gz", "sha256": "f3c2b4c562abfb1d182fb950503285637e7dee6c9b45baa2a06172923c76a4c7"},
    "linux": {"file": "sfizz-1.2.3.tar.gz", "sha256": "a9339eac7620d7f0f6b44bdfe860680fab73e66efad4b5f15b21198dd9436822",
              "build": True},  # no Linux binary upstream: build the official source release
}
SFIZZ_URL = "https://github.com/sfztools/sfizz/releases/download/1.2.3/"

# Surge XT factory patches used by compose, per (role, style). Fetched from the Surge repo at the
# release tag; verified by git blob sha1 (filled in from the tree at that tag).
SURGE_PATCHES = {
    ("bass", "offbeat"): "Basses/Bass 1", ("bass", "rolling"): "Basses/Square Bass", ("bass", "reese"): "Basses/Wide Bassline",
    ("bass", "808"): "Basses/Sub 1", ("bass", "root"): "Basses/Mellow", ("bass", "drone"): "Basses/Rumble",
    ("chords", "supersaw"): "Polysynths/Anthemish 1", ("chords", "stabs"): "Polysynths/Smooth Stabs",
    ("chords", "pad"): "Pads/Pad 1", ("chords", "keys"): "Keys/EP 1", ("pad", "*"): "Pads/MKS-70 Warm Pad",
    ("lead", "anthem"): "Leads/Classic Lead 1", ("lead", "pluck"): "Plucks/Nice Pluck 1", ("lead", "bell"): "Plucks/Bell 1",
    ("lead", "sparse"): "Leads/Sine Lead", ("lead", "arp"): "Plucks/Saw Pluck", ("arp", "*"): "Plucks/Saw Pluck",
}
EXTRA_PATCHES = ["Basses/FM Bass 1", "Basses/Sub 2", "Leads/Saw Octaves", "Leads/Sync Lead", "Pads/Pad 3", "Pads/Bright",
                 "Polysynths/Juno-60 Strings", "Polysynths/Super Jupiter", "Keys/Organ 1", "Plucks/FM Pluck"]
SURGE_RAW = f"https://raw.githubusercontent.com/surge-synthesizer/surge/{SURGE_TAG}/resources/data/patches_factory/"
SURGE_FXP_HEADER = 60   # CLAP state = .fxp without its 60-byte VST2 chunk header

# CC0 sample set: VS Chamber Orchestra 2 Community Edition (Versilian Studios), pinned commit.
VSCO_RAW = f"https://raw.githubusercontent.com/sgossner/VSCO-2-CE/{VSCO_COMMIT}/"
PIANO_KEYS = {i: 21 + 2 * i for i in range(44)} | {44: 108}
SFZ_SETS = {
    "piano": {"title": "Upright Piano (VSCO 2 CE, Ivy Audio / Versilian Studios, CC0)",
              "files": [f"Keys/Upright Piano/Player_dyn{d}_rr1_{i:03d}.wav" for d in (1, 3) for i in range(0, 45, 4)]},
    "strings": {"title": "Violin Section sustain (VSCO 2 CE, Versilian Studios, CC0)",
                "files": [f"Strings/Violin Section/susVib/VlnEns_susVib_{n}_v2.wav"
                          for n in ("G2", "A2", "B2", "D3", "F#3", "A3", "C4", "E4", "G4", "B4", "D5")]},
}
NOTE_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


from .ops import OpError


class KitError(OpError):
    pass


def os_name() -> str:
    return "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"


def kit_dir() -> Path:
    env = os.environ.get("PLAINDAW_KIT")
    if env:
        return Path(env)
    app = Path(__file__).resolve().parent.parent      # the PlainDAW/AIDAW folder (source checkout)
    if os.access(app, os.W_OK) or sys.platform == "win32":
        return app / "kit"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "plaindaw" / "kit"


def registry() -> dict:
    p = kit_dir() / "kit.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except ValueError:
        return {}


def _save_registry(reg: dict) -> None:
    d = kit_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "kit.json").write_text(json.dumps(reg, indent=2))


def _abs(rel: str | None) -> Path | None:
    return (kit_dir() / rel) if rel else None


def installed() -> dict:
    """What is usable right now: {surge: bool, sfizz: bool, sfz: [names]}."""
    reg = registry()
    s = reg.get("surge") or {}
    z = reg.get("sfizz") or {}
    return {"surge": bool(s.get("clap") and _abs(s["clap"]).exists()),
            "sfizz": bool(z.get("render") and _abs(z["render"]).exists()),
            "sfz": sorted(k for k, v in (reg.get("sfz") or {}).items() if not k.startswith("_") and _abs(v["path"]).exists())}


def plugin_dirs() -> list[Path]:
    d = kit_dir() / "surge"
    return [d] if d.exists() else []


def surge_portable_dirs(plugin_dir: Path) -> None:
    """Surge XT portable mode: SurgeXTData / SurgeXTUserData folders beside the plugins.
    On Windows this is what makes the plugins-only build start (it otherwise looks for an
    installed data folder) and it keeps Surge's user folder inside the kit instead of Documents."""
    if sys.platform != "win32":
        return
    for nm in ("SurgeXTData", "SurgeXTUserData"):
        (Path(plugin_dir) / nm).mkdir(parents=True, exist_ok=True)


def surge_ref() -> str:
    """Plugin ref for Surge XT: CLAP when the CLAP host can run here, else the VST3 (same patch states)."""
    from .native_build import clap_host_available
    s = registry().get("surge") or {}
    if clap_host_available():
        return f"clap:{SURGE_ID}"
    if s.get("vst3") and _abs(s["vst3"]).exists():
        return "vst3:Surge XT"
    return "vst3:Surge XT"


def resolve_ref(ref: str) -> str | None:
    """Fast path for kit plugins: plugin ref -> bundle path."""
    s = registry().get("surge") or {}
    if s.get("clap") and "surge" in ref.lower():
        surge_portable_dirs(_abs(s["clap"]).parent)
    if ref == f"clap:{SURGE_ID}" and s.get("clap") and _abs(s["clap"]).exists():
        return str(_abs(s["clap"]))
    if ref.lower() == "vst3:surge xt" and s.get("vst3") and _abs(s["vst3"]).exists():
        return str(_abs(s["vst3"]))
    return None


def sfizz_render() -> Path | None:
    z = registry().get("sfizz") or {}
    p = _abs(z.get("render"))
    return p if p and p.exists() else None


def sfz_path(name: str) -> Path | None:
    v = (registry().get("sfz") or {}).get(name.lower())
    p = _abs(v["path"]) if v else None
    return p if p and p.exists() else None


def surge_presets() -> dict:
    return (registry().get("surge") or {}).get("presets") or {}


# ------------------------------------------------------------------ install
def _pins() -> dict:
    return json.loads((Path(__file__).parent / "kit_pins.json").read_text())


def _git_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _fetch(url: str, dest: Path, log, md5=None, sha256=None, git_sha1=None) -> Path:
    """Download to dest (skips if already present and verified)."""
    def ok(p: Path) -> bool:
        if not p.exists():
            return False
        data = p.read_bytes()
        if md5 and hashlib.md5(data).hexdigest() != md5:
            return False
        if sha256 and hashlib.sha256(data).hexdigest() != sha256:
            return False
        if git_sha1 and _git_sha1(data) != git_sha1:
            return False
        return bool(md5 or sha256 or git_sha1)
    if ok(dest):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "PlainDAW-kit"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if not ok(tmp) and (md5 or sha256 or git_sha1):
        data = tmp.read_bytes()
        tmp.unlink()
        raise KitError(f"checksum mismatch for {url} (md5 {hashlib.md5(data).hexdigest()}, "
                       f"sha256 {hashlib.sha256(data).hexdigest()})")
    tmp.replace(dest)
    return dest


def _official_md5(name: str) -> str | None:
    """Cross-check against the md5sum.txt published with the Surge release."""
    try:
        with urllib.request.urlopen(SURGE_URL + "md5sum.txt", timeout=30) as r:
            for line in r.read().decode().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].endswith(name):
                    return parts[0]
    except Exception:
        return None
    return None


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path):
    dest = dest.resolve()
    for m in tf.getmembers():
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest)) or m.issym() and Path(m.linkname).is_absolute():
            raise KitError(f"unsafe path in archive: {m.name}")
    tf.extractall(dest)


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path):
    dest = dest.resolve()
    for n in zf.namelist():
        if not str((dest / n).resolve()).startswith(str(dest)):
            raise KitError(f"unsafe path in archive: {n}")
    zf.extractall(dest)


def install_surge(osn: str, log) -> dict:
    a = SURGE_ASSETS[osn]
    root = kit_dir() / "surge"
    off = _official_md5(a["file"])
    if off and off != a["md5"]:
        raise KitError(f"Surge release md5 changed upstream ({off}); refusing to install")
    arc = _fetch(SURGE_URL + a["file"], kit_dir() / "downloads" / a["file"], log, md5=a["md5"], sha256=a["sha256"])
    log(f"  verified {a['file']} (md5{' + sha256' if a['sha256'] else ''}"
        f"{', matches the release md5sum.txt' if off else ''})")
    out = root / "plugins"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if arc.suffix == ".zip":
        with zipfile.ZipFile(arc) as zf:
            _safe_extract_zip(zf, out)
    else:
        with tarfile.open(arc) as tf:
            _safe_extract_tar(tf, out)
    for extra in list(out.rglob("*.lv2")) + list(out.rglob("surge-xt-cli*")):
        shutil.rmtree(extra) if extra.is_dir() else extra.unlink()   # not used by PlainDAW
    clap = next(iter(sorted(out.rglob("Surge XT.clap"))), None)
    vst3 = next(iter(sorted(out.rglob("Surge XT.vst3"))), None)
    if clap is None:
        raise KitError("Surge XT.clap not found in the release archive")
    surge_portable_dirs(clap.parent)
    pins = _pins()["surge_patches"]
    presets = {}
    for name, pin in pins.items():
        fxp = _fetch(SURGE_RAW + urllib.request.quote(name) + ".fxp", root / "patches" / f"{name}.fxp", log,
                     git_sha1=pin["git_sha1"])
        data = fxp.read_bytes()
        state = root / "states" / f"{name}.bin"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_bytes(data[SURGE_FXP_HEADER:])
        presets[name] = {"fxp": str(fxp.relative_to(kit_dir())), "state": str(state.relative_to(kit_dir()))}
    log(f"  {len(presets)} Surge factory patches verified")
    rel = lambda p: str(Path(p).relative_to(kit_dir())) if p else None
    return {"version": SURGE_VERSION, "id": SURGE_ID, "clap": rel(clap), "vst3": rel(vst3), "presets": presets,
            "license": "GPL-3.0-or-later", "source": SURGE_URL + a["file"]}


def install_sfizz(osn: str, log, jobs: int = 0) -> dict:
    a = SFIZZ_ASSETS[osn]
    root = kit_dir() / "sfizz"
    arc = _fetch(SFIZZ_URL + a["file"], kit_dir() / "downloads" / a["file"], log, sha256=a["sha256"])
    log(f"  verified {a['file']} (sha256)")
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if a.get("build"):
        src = root / "src"
        if not (src / "CMakeLists.txt").exists():
            if src.exists():
                shutil.rmtree(src)
            src.mkdir(parents=True)
            with tarfile.open(arc) as tf:
                _safe_extract_tar(tf, src)
            inner = [p for p in src.iterdir() if p.is_dir()]
            if len(inner) == 1 and (inner[0] / "CMakeLists.txt").exists():
                for p in inner[0].iterdir():
                    p.rename(src / p.name)
                inner[0].rmdir()
        cmake = shutil.which("cmake")
        if not cmake or not (shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")):
            raise KitError("sfizz has no official Linux binary; building it needs cmake and a C++ compiler "
                           "(the built-in SFZ player is used meanwhile)")
        build = root / "build"
        gen = ["-G", "Ninja"] if shutil.which("ninja") else []
        log("  building sfizz_render from the official source release (about a minute)")
        subprocess.run([cmake, "-S", str(src), "-B", str(build), *gen, "-DCMAKE_BUILD_TYPE=Release", "-DSFIZZ_JACK=OFF",
                        "-DSFIZZ_RENDER=ON", "-DENABLE_LTO=OFF", "-DSFIZZ_SHARED=OFF", "-DSFIZZ_GIT_SUBMODULE_CHECK=OFF"],
                       check=True, capture_output=True)
        subprocess.run([cmake, "--build", str(build), "--target", "sfizz_render", "-j", str(jobs or os.cpu_count() or 4)],
                       check=True, capture_output=True)
        exe = next(iter(build.rglob("sfizz_render")), None)
        if exe is None:
            raise KitError("sfizz build finished but sfizz_render was not produced")
        shutil.copy2(exe, bin_dir / "sfizz_render")
        render = bin_dir / "sfizz_render"
        shutil.rmtree(build, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)
    elif arc.suffix == ".zip":
        with zipfile.ZipFile(arc) as zf:
            for n in zf.namelist():
                if n.endswith(("sfizz_render.exe", "sfizz.dll")):
                    (bin_dir / Path(n).name).write_bytes(zf.read(n))
        render = bin_dir / "sfizz_render.exe"
    else:
        with tarfile.open(arc) as tf:
            for m in tf.getmembers():
                if m.isfile() and (m.name.endswith("/bin/sfizz_render") or ".dylib" in m.name):
                    (bin_dir / Path(m.name).name).write_bytes(tf.extractfile(m).read())
        render = bin_dir / "sfizz_render"
    if os_name() != "windows":
        render.chmod(0o755)
    return {"version": SFIZZ_VERSION, "render": str(render.relative_to(kit_dir())), "license": "BSD-2-Clause",
            "source": SFIZZ_URL + a["file"]}


def _note_num(name: str) -> int:
    pc = NOTE_PC[name[0]] + (1 if "#" in name else -1 if "b" in name[1:] else 0)
    octave = int(name.lstrip("ABCDEFG#b"))
    return 12 * (octave + 1) + pc


def write_sfz(name: str, files: list[Path]) -> str:
    """SFZ mapping for the downloaded samples (regions split halfway between sampled notes)."""
    lines = [f"// {SFZ_SETS[name]['title']}", "// mapping generated by PlainDAW kit install", ""]
    if name == "piano":
        lines += ["<global> ampeg_release=0.6 amp_veltrack=85", ""]
        for dyn, lo, hi in ((1, 1, 72), (3, 73, 127)):
            keys = sorted((PIANO_KEYS[int(f.stem[-3:])], f) for f in files if f"_dyn{dyn}_" in f.name)
            lines.append(f"<group> lovel={lo} hivel={hi}")
            for i, (k, f) in enumerate(keys):
                klo = 0 if i == 0 else (keys[i - 1][0] + k) // 2 + 1
                khi = 127 if i == len(keys) - 1 else (k + keys[i + 1][0]) // 2
                lines.append(f"<region> sample={f.name} pitch_keycenter={k} lokey={klo} hikey={khi}")
            lines.append("")
    else:
        lines += ["<global> ampeg_attack=0.08 ampeg_release=0.5 loop_mode=no_loop", "<group>"]
        keys = sorted((_note_num(f.stem.split("_")[2]), f) for f in files)
        for i, (k, f) in enumerate(keys):
            klo = 0 if i == 0 else (keys[i - 1][0] + k) // 2 + 1
            khi = 127 if i == len(keys) - 1 else (k + keys[i + 1][0]) // 2
            lines.append(f"<region> sample={f.name} pitch_keycenter={k} lokey={klo} hikey={khi}")
    return "\n".join(lines) + "\n"


def install_sfz(names: list[str], log) -> dict:
    pins = _pins()["vsco"]
    out = {}
    lic = _fetch(VSCO_RAW + "LICENSE", kit_dir() / "sfz" / "VSCO-2-CE-LICENSE.txt", log, git_sha1=pins["LICENSE"]["git_sha1"])
    for name in names:
        if name not in SFZ_SETS:
            raise KitError(f"unknown SFZ set {name!r} (choose {', '.join(SFZ_SETS)})")
        d = kit_dir() / "sfz" / name
        files = []
        for f in SFZ_SETS[name]["files"]:
            files.append(_fetch(VSCO_RAW + urllib.request.quote(f), d / Path(f).name, log, git_sha1=pins[f]["git_sha1"]))
        sfz = d / f"{name}.sfz"
        sfz.write_text(write_sfz(name, files))
        out[name] = {"path": str(sfz.relative_to(kit_dir())), "title": SFZ_SETS[name]["title"], "license": "CC0-1.0",
                     "source": f"https://github.com/sgossner/VSCO-2-CE/tree/{VSCO_COMMIT}"}
        log(f"  {name}: {len(files)} samples verified, mapping {sfz.name}")
    out["_license_file"] = {"path": str(lic.relative_to(kit_dir()))}
    return out


def install(components=None, log=print, force=False) -> dict:
    comps = [c.strip().lower() for c in (components or ["surge", "sfizz", "sfz"])]
    osn = os_name()
    if osn == "windows" and "program files" in str(kit_dir()).lower():
        raise KitError("refusing to install the kit under Program Files; set PLAINDAW_KIT to a folder you own")
    if platform.machine().lower() not in ("x86_64", "amd64", "arm64", "aarch64"):
        log(f"warning: untested CPU {platform.machine()}")
    log(f"kit folder: {kit_dir()}  (os: {osn})")
    reg = registry() if not force else {}
    reg["version"] = 1
    report = {"kitDir": str(kit_dir()), "os": osn, "installed": [], "failed": {}}
    for c in comps:
        try:
            if c == "surge":
                log("Surge XT " + SURGE_VERSION)
                reg["surge"] = install_surge(osn, log)
            elif c == "sfizz":
                log("sfizz " + SFIZZ_VERSION)
                reg["sfizz"] = install_sfizz(osn, log)
            elif c in ("sfz", "samples"):
                log("SFZ set (CC0)")
                reg["sfz"] = {k: v for k, v in install_sfz(list(SFZ_SETS), log).items()}
            elif c in SFZ_SETS:
                reg.setdefault("sfz", {}).update(install_sfz([c], log))
            else:
                raise KitError(f"unknown component {c!r} (surge, sfizz, sfz, piano, strings)")
            report["installed"].append(c)
        except (KitError, OSError, subprocess.CalledProcessError, urllib.error.URLError) as e:
            report["failed"][c] = str(e)
            log(f"  FAILED: {e}")
        _save_registry(reg)
    from .plugins import scan as _scan
    _scan.reset_cache()
    report["status"] = installed()
    return report


def status() -> dict:
    reg = registry()
    st = installed()
    return {"kitDir": str(kit_dir()), "os": os_name(), **st,
            "surgeVersion": (reg.get("surge") or {}).get("version"), "sfizzVersion": (reg.get("sfizz") or {}).get("version"),
            "surgePresets": sorted(surge_presets()),
            "hint": None if st["surge"] and st["sfizz"] and st["sfz"] else "run `plaindaw kit install`"}


def clean() -> dict:
    """Delete cached downloads (the installed kit keeps working)."""
    d = kit_dir() / "downloads"
    size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0
    shutil.rmtree(d, ignore_errors=True)
    return {"freedMB": round(size / 1e6, 1)}


def remove() -> dict:
    d = kit_dir()
    if d.exists() and (d / "kit.json").exists():
        shutil.rmtree(d)
        return {"removed": str(d)}
    return {"removed": None}


# ------------------------------------------------------------------ used by compose / instrument add
def _copy_state(src: Path, project_dir: Path | None) -> str | None:
    if project_dir is None:
        return None
    data = src.read_bytes()
    h = hashlib.sha256(data).hexdigest()[:10]
    rel = f"states/kit_{src.stem.replace(' ', '_')}_{h}.bin"
    dst = Path(project_dir) / rel
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    return rel


def _find_preset(name: str) -> str | None:
    pres = surge_presets()
    if name in pres:
        return name
    low = name.lower()
    for k in pres:
        if k.lower() == low or k.split("/")[-1].lower() == low:
            return k
    for k in pres:
        if low in k.lower():
            return k
    return None


def surge_preset_for(role, style, preset=None, project_dir=None) -> dict | None:
    if not installed()["surge"]:
        return None
    name = _find_preset(preset) if preset else (SURGE_PATCHES.get((role, style)) or SURGE_PATCHES.get((role, "*")))
    if not name or name not in surge_presets():
        return None
    state = _copy_state(_abs(surge_presets()[name]["state"]), project_dir)
    return {"plugin": surge_ref(), "state": state, "name": name}


def instrument_spec(name: str, preset=None, sfz=None, project_dir=None) -> dict:
    name = name.lower()
    if name in ("surge", "surge-xt", "surgext", "surge xt"):
        if not installed()["surge"]:
            raise KitError("Surge XT is not installed: run `plaindaw kit install`")
        spec = {"plugin": surge_ref(), "label": "Surge XT (init patch)"}
        if preset:
            k = _find_preset(preset)
            if not k:
                raise KitError(f"no Surge preset like {preset!r} in the kit (see `plaindaw kit list`)")
            spec["state"] = _copy_state(_abs(surge_presets()[k]["state"]), project_dir)
            spec["label"] = f"Surge XT: {k}"
        return spec
    if name in ("sfizz", "sfz", "sampler-sfz"):
        which = (sfz or "piano").lower()
        if sfz_path(which) is None and not Path(which).suffix == ".sfz":
            raise KitError(f"SFZ set {which!r} is not installed: run `plaindaw kit install` (sets: {', '.join(SFZ_SETS)})")
        engine = "sfizz" if installed()["sfizz"] else "built-in SFZ player"
        return {"plugin": f"sfz:{which}", "label": f"{engine}: {which}"}
    raise KitError(f"unknown kit instrument {name!r} (surge, sfizz)")
