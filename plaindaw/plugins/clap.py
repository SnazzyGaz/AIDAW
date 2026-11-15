"""ctypes wrapper around native/src/pdclap.c (offline CLAP host)."""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..native_build import ensure_built

_lib = None


class _Ev(C.Structure):
    _fields_ = [("type", C.c_uint16), ("time", C.c_uint32), ("key", C.c_int32),
                ("channel", C.c_int32), ("value", C.c_double)]


def lib():
    global _lib
    if _lib is None:
        L = C.CDLL(str(ensure_built()["host"]))
        L.pdc_list.argtypes = [C.c_char_p, C.c_char_p, C.c_int]; L.pdc_list.restype = C.c_int
        L.pdc_load.argtypes = [C.c_char_p, C.c_char_p]; L.pdc_load.restype = C.c_void_p
        L.pdc_ok.argtypes = [C.c_void_p]; L.pdc_ok.restype = C.c_int
        L.pdc_last_error.argtypes = [C.c_void_p]; L.pdc_last_error.restype = C.c_char_p
        L.pdc_ports.argtypes = [C.c_void_p, C.c_int, C.c_int]; L.pdc_ports.restype = C.c_int
        L.pdc_latency.argtypes = [C.c_void_p]; L.pdc_latency.restype = C.c_int
        L.pdc_activate.argtypes = [C.c_void_p, C.c_double, C.c_int]; L.pdc_activate.restype = C.c_int
        L.pdc_param_count.argtypes = [C.c_void_p]; L.pdc_param_count.restype = C.c_int
        L.pdc_param_info.argtypes = [C.c_void_p, C.c_int, C.c_char_p, C.c_int, C.POINTER(C.c_double),
                                     C.POINTER(C.c_double), C.POINTER(C.c_double), C.POINTER(C.c_uint32)]
        L.pdc_param_info.restype = C.c_int
        L.pdc_param_get.argtypes = [C.c_void_p, C.c_uint32, C.POINTER(C.c_double)]; L.pdc_param_get.restype = C.c_int
        L.pdc_flush_params.argtypes = [C.c_void_p, C.POINTER(_Ev), C.c_int]; L.pdc_flush_params.restype = C.c_int
        L.pdc_process.argtypes = [C.c_void_p, C.POINTER(C.c_float), C.c_int, C.POINTER(C.c_float), C.c_int,
                                  C.c_int, C.POINTER(_Ev), C.c_int]
        L.pdc_process.restype = C.c_int
        L.pdc_state_save.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]; L.pdc_state_save.restype = C.c_long
        L.pdc_state_load.argtypes = [C.c_void_p, C.c_char_p, C.c_long]; L.pdc_state_load.restype = C.c_int
        L.pdc_free.argtypes = [C.c_void_p]
        L.pdc_destroy.argtypes = [C.c_void_p]
        _lib = L
    return _lib


@dataclass
class ClapParam:
    id: int
    name: str
    min: float
    max: float
    default: float


def _binary(path) -> str:
    """macOS .clap bundles are folders; the loadable binary is Contents/MacOS/<name>."""
    p = Path(path)
    if p.is_dir():
        mac = p / "Contents" / "MacOS"
        if mac.is_dir():
            for f in sorted(mac.iterdir()):
                if f.is_file():
                    return str(f)
    return str(p)


def list_plugins(path: str | Path) -> list[dict]:
    buf = C.create_string_buffer(65536)
    n = lib().pdc_list(_binary(path).encode(), buf, len(buf))
    if n < 0:
        raise RuntimeError(f"CLAP list failed: {buf.value.decode(errors='replace')}")
    out = []
    for line in buf.value.decode(errors="replace").splitlines():
        pid, name, vendor, version, feats = (line.split("\t") + [""] * 5)[:5]
        f = [x for x in feats.split(",") if x]
        out.append({"id": pid, "name": name, "vendor": vendor, "version": version, "features": f,
                    "instrument": "instrument" in f})
    return out


class ClapPlugin:
    """One CLAP plugin instance for offline processing."""

    def __init__(self, path: str | Path, plugin_id: str | None = None):
        self.path = str(path)
        self._h = lib().pdc_load(_binary(self.path).encode(), (plugin_id or "").encode())
        if not lib().pdc_ok(self._h):
            err = lib().pdc_last_error(self._h).decode(errors="replace")
            lib().pdc_destroy(self._h)
            self._h = None
            raise RuntimeError(f"CLAP load failed ({path}): {err}")
        self.in_channels = max(0, lib().pdc_ports(self._h, 1, 0))
        self.out_channels = max(0, lib().pdc_ports(self._h, 0, 0))
        self.params = self._read_params()
        self._by_name = {p.name.lower(): p for p in self.params}
        self._active = False

    @property
    def is_instrument(self) -> bool:
        return self.in_channels == 0

    def _read_params(self) -> list[ClapParam]:
        out = []
        for i in range(lib().pdc_param_count(self._h)):
            name = C.create_string_buffer(512)
            mn, mx, df = C.c_double(), C.c_double(), C.c_double()
            fl = C.c_uint32()
            pid = lib().pdc_param_info(self._h, i, name, 512, C.byref(mn), C.byref(mx), C.byref(df), C.byref(fl))
            if pid >= 0:
                out.append(ClapParam(pid, name.value.decode(errors="replace"), mn.value, mx.value, df.value))
        return out

    def find_param(self, key: str | int) -> ClapParam:
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            for p in self.params:
                if p.id == int(key):
                    return p
        k = str(key).lower()
        if k in self._by_name:
            return self._by_name[k]
        for p in self.params:  # suffix match ("Gain" for "Global/Gain")
            if p.name.lower().endswith("/" + k) or p.name.lower().replace(" ", "_") == k:
                return p
        raise KeyError(f"CLAP param not found: {key}")

    def param_value(self, p: ClapParam) -> float:
        v = C.c_double()
        lib().pdc_param_get(self._h, p.id, C.byref(v))
        return v.value

    def set_params(self, values: dict) -> None:
        evs = []
        for k, v in values.items():
            p = self.find_param(k)
            evs.append((2, 0, p.id, 0, float(v)))
        if evs:
            arr = (_Ev * len(evs))(*[_Ev(*e) for e in evs])
            lib().pdc_flush_params(self._h, arr, len(evs))

    def get_state(self) -> bytes | None:
        ptr = C.c_void_p()
        n = lib().pdc_state_save(self._h, C.byref(ptr))
        if n < 0:
            return None
        data = C.string_at(ptr, n)
        lib().pdc_free(ptr)
        return data

    def set_state(self, data: bytes) -> bool:
        return bool(lib().pdc_state_load(self._h, data, len(data)))

    def activate(self, sample_rate: float, block: int) -> None:
        if not lib().pdc_activate(self._h, float(sample_rate), int(block)):
            raise RuntimeError(lib().pdc_last_error(self._h).decode())
        self._active = True
        self.sample_rate, self.block = sample_rate, block

    def process(self, audio: np.ndarray | None, n_frames: int, events: list[tuple] | None = None,
                param_curve: dict | None = None) -> np.ndarray:
        """Render n_frames. audio: (ch, n) float or None. events: list of
        (frame, type, key/param_id, channel, value) with type 0=on,1=off,2=param.
        Returns (2, n_frames) float32."""
        if not self._active:
            raise RuntimeError("activate() first")
        B = self.block
        out = np.zeros((2, n_frames), dtype=np.float32)
        ev = sorted(events or [], key=lambda e: (e[0], 0 if e[1] == 2 else (1 if e[1] == 1 else 2)))
        ei = 0
        in_ch = 0 if audio is None else audio.shape[0]
        for start in range(0, n_frames, B):
            n = min(B, n_frames - start)
            blk_ev = []
            while ei < len(ev) and ev[ei][0] < start + n:
                f, t, k, ch, v = ev[ei]
                blk_ev.append(_Ev(t, max(0, int(f) - start), int(k), int(ch), float(v)))
                ei += 1
            if in_ch:
                inb = np.ascontiguousarray(audio[:, start:start + n], dtype=np.float32)
                in_ptr = inb.ctypes.data_as(C.POINTER(C.c_float))
            else:
                inb = None
                in_ptr = None
            ob = np.zeros((2, n), dtype=np.float32)
            arr = (_Ev * max(1, len(blk_ev)))(*blk_ev) if blk_ev else (_Ev * 1)()
            st = lib().pdc_process(self._h, in_ptr, in_ch, ob.ctypes.data_as(C.POINTER(C.c_float)), 2, n,
                                   arr, len(blk_ev))
            if st < 0 or st == 0:  # CLAP_PROCESS_ERROR == 0
                raise RuntimeError(f"CLAP process error: {lib().pdc_last_error(self._h).decode()}")
            out[:, start:start + n] = ob
        return out

    def close(self) -> None:
        if self._h:
            lib().pdc_destroy(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
