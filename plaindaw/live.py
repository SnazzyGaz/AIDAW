"""Live playback and live MIDI.

How it works (honest version):
  * Timeline playback is *render-ahead*, not per-block plugin processing. A background thread renders
    the song (or, with loop on, up to the loop end, with the reverb tail folded back to the loop start)
    with the same offline renderer that writes files, and the audio callback streams that buffer.
  * Re-renders are incremental: instrument outputs, audio clips and plugin results are cached by
    everything that feeds them, so after an edit only the changed tracks' sources (and the cheap mixing
    and built-in effects) are redone. The new buffer is crossfaded in at the current play position, so a
    change is heard as soon as its render finishes (fast for built-ins; Surge/VST3 tracks take longer).
  * Live notes (MIDI keyboard, piano-roll keys) are real time: they are synthesised with the track's
    built-in instrument (synth, drums, sampler) and mixed in the callback. Plugin and SFZ tracks are
    previewed with a stand-in built-in synth (flagged as such in the status).
  * Audio goes through PortAudio (sounddevice): WASAPI / DirectSound / MME, and ASIO when
    PLAINDAW_ASIO=1 is set before the first audio call. MIDI input uses mido + python-rtmidi.
  * Without an audio device (servers, tests) a null output runs the same callback.
"""
from __future__ import annotations

import collections
import copy
import os
import threading
import time
from pathlib import Path

import numpy as np

from . import dsp
from .timebase import TimeMap, note_number

ENGINES: dict = {}            # session id -> Engine (one engine per project per process)
SERVER_FILE = ".live-server.json"


def _sd():
    if os.environ.get("PLAINDAW_ASIO", "").strip() not in ("", "0") and "SD_ENABLE_ASIO" not in os.environ:
        os.environ["SD_ENABLE_ASIO"] = "1"
    import sounddevice as sd  # noqa: WPS433
    return sd


class Voice:
    __slots__ = ("data", "pos", "delay", "track", "key", "fade_from", "fade_len", "click")

    def __init__(self, data, track=None, key=None, delay=0, click=False):
        self.data = data          # (m, 2) float32
        self.pos = 0
        self.delay = int(delay)   # frames to wait inside the first block
        self.track, self.key, self.click = track, key, click
        self.fade_from = None     # sample index where a note-off release starts
        self.fade_len = 0

    def release(self, n):
        if self.fade_from is None:
            self.fade_from = self.pos
            self.fade_len = max(32, int(n))

    def render(self, frames) -> np.ndarray | None:
        out = np.zeros((frames, 2), np.float32)
        d = min(self.delay, frames)
        self.delay -= d
        take = frames - d
        if take <= 0:
            return out
        seg = self.data[self.pos:self.pos + take]
        if self.fade_from is not None:
            idx = np.arange(self.pos, self.pos + len(seg))
            g = np.clip(1.0 - (idx - self.fade_from) / self.fade_len, 0.0, 1.0).astype(np.float32)
            seg = seg * g[:, None]
            if len(seg) and g[-1] <= 0:
                self.pos = len(self.data)
        out[d:d + len(seg)] = seg
        self.pos += len(seg)
        return out

    @property
    def done(self):
        return self.pos >= len(self.data) and self.delay <= 0


def _soft_clip(x, knee=0.9):
    """Leave everything under `knee` alone; overs bend smoothly towards 1.0 (no hard clipping)."""
    a = np.abs(x)
    m = a > knee
    if m.any():
        x[m] = np.sign(x[m]) * (knee + (1 - knee) * np.tanh((a[m] - knee) / (1 - knee)))
    return x


def _click(sr, hi):
    n = int(0.03 * sr)
    t = np.arange(n) / sr
    s = np.sin(2 * np.pi * (2000 if hi else 1300) * t) * np.exp(-t / 0.006) * (0.5 if hi else 0.32)
    return np.stack([s, s], 1).astype(np.float32)


class Engine:
    def __init__(self, session, output: str = "auto", blocksize: int | None = None, device=None):
        self.s = session
        self.sr = int(session.doc.get("settings", {}).get("sampleRate", 48000))
        self.blocksize = int(blocksize or os.environ.get("PLAINDAW_BLOCKSIZE", 512))
        self.device = device if device is not None else (os.environ.get("PLAINDAW_AUDIO_DEVICE") or None)
        self.output_req = output
        self.output_kind = None          # "portaudio" | "null" | "manual"
        self.stream = None
        self.lock = threading.RLock()
        self.playing = False
        self.pos = 0                     # next sample to send (song timeline)
        self.loop_on = bool(session.doc["transport"].get("loop"))
        self.metronome = False
        self.buf = None                  # (n, 2) float32 song timeline from 0
        self.buf_meta = {}
        self.ls = self.le = 0            # loop range (samples) of the current buffer
        self.beats = np.zeros(0, np.int64)
        self.downbeats = set()
        self.tm = TimeMap(session.doc["transport"])
        self._next = None                # buffer waiting to be swapped in
        self._fade_old = None
        self.voices: list[Voice] = []
        self.cmds = collections.deque()
        self.cache: dict = {}
        self.render_state = "idle"
        self.render_info: dict = {}
        self.cpu = 0.0
        self.xruns = 0
        self.latency = 0.0
        self.error = None
        self.active = {}                 # (track, key) -> vel  (notes held right now)
        self.midi_port = None
        self.midi_in = None
        self.midi_track = None
        self.rec_on = False
        self.rec_quant = 0.25
        self.rec_track = None
        self.rec_pending = {}
        self.rec_notes = []
        self._clicks = (_click(self.sr, True), _click(self.sr, False))
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._doc_version = -1
        self._want_version = 0
        self._done_version = -1
        self._null_thread = None
        session.listeners.append(self._on_change)
        self._worker = threading.Thread(target=self._render_loop, daemon=True, name="aidaw-render")
        self._worker.start()
        self.invalidate()

    # ------------------------------------------------------------ rendering
    def _on_change(self, ev):
        self.invalidate()

    def invalidate(self):
        self._want_version += 1
        self._dirty.set()

    def _region(self, doc, tm):
        from .render import song_end_qn
        end = max(song_end_qn(doc, tm, self.s.dir), 4.0)
        lp = doc["transport"].get("loop")
        if self.loop_on and lp:
            ls_q, le_q = tm.qn(lp["start"]), tm.qn(lp["end"])
        elif self.loop_on:
            bar = tm.bars_to_qn(1)
            ls_q, le_q = 0.0, float(np.ceil(end / bar) * bar)
        else:
            ls_q, le_q = None, None
        return end, ls_q, le_q

    def _render_once(self):
        from .render import render
        with self.s.lock:
            try:
                self.s.reload_if_changed()
            except Exception:
                pass
            doc = copy.deepcopy(self.s.doc)
        tm = TimeMap(doc["transport"])
        end_q, ls_q, le_q = self._region(doc, tm)
        r_end = le_q if le_q is not None else end_q
        t0 = time.perf_counter()
        self.render_state = "rendering"
        res = render(doc, self.s.dir, sr=self.sr, end=r_end, include_tail=True, cache=self.cache)
        took = time.perf_counter() - t0
        mix = np.ascontiguousarray(res.mix.T, dtype=np.float32)
        if le_q is not None:
            ls, le = int(round(tm.sec(ls_q) * self.sr)), int(round(tm.sec(le_q) * self.sr))
            le = min(le, len(mix))
            body = mix[:le].copy()
            tail = mix[le:]
            L = le - ls
            if L > 0 and len(tail):          # fold the tail (reverb, releases) back onto the loop start
                for k in range(0, len(tail), L):
                    seg = tail[k:k + L]
                    body[ls:ls + len(seg)] += seg
            mix = _soft_clip(body)
        else:
            ls = le = 0
        beats, down = [], set()
        q, last = 0.0, (le_q if le_q is not None else end_q) + 4
        bar_starts = set()
        b = 1
        while True:
            bq = tm.qn(f"{b}.1.1")
            if bq > last:
                break
            bar_starts.add(round(bq, 6)); b += 1
        while q <= last:
            sm = int(round(tm.sec(q) * self.sr))
            beats.append(sm)
            if round(q, 6) in bar_starts:
                down.add(sm)
            q += float(tm._sig_for_qn(q).qn_per_beat)
        self.cache = getattr(res, "cache_out", {})
        stats = getattr(res, "cache_stats", {})
        info = {"renderSec": round(took, 3), "seconds": round(len(mix) / self.sr, 3),
                "cachedParts": stats.get("hits", 0), "renderedParts": stats.get("misses", 0),
                "warnings": list(res.warnings)[:5]}
        with self.lock:
            self._next = (mix, ls, le, np.array(beats, np.int64), down, tm, info)
            if self.buf is None or self.output_kind in (None, "manual") or not self.playing:
                self._swap(crossfade=False)

    def _render_loop(self):
        while not self._stop.is_set():
            self._dirty.wait(0.5)
            if self._stop.is_set():
                return
            if not self._dirty.is_set():
                continue
            time.sleep(0.12)                 # debounce bursts of edits (fader drags, batches)
            self._dirty.clear()
            want = self._want_version
            try:
                self._render_once()
                self.render_state = "ready" if want == self._want_version else "rendering"
                self.error = None
            except Exception as e:  # noqa: BLE001
                self.render_state = "error"
                self.error = f"{type(e).__name__}: {e}"
            self._done_version = want

    def wait_ready(self, timeout=60.0):
        t = time.time() + timeout
        while time.time() < t:
            if (self._done_version == self._want_version and self.render_state in ("ready", "error")
                    and self._next is None):
                return self.render_state == "ready"
            time.sleep(0.02)
        return False

    def _swap(self, crossfade=True):
        nxt, self._next = self._next, None
        if nxt is None:
            return
        mix, ls, le, beats, down, tm, info = nxt
        if crossfade and self.buf is not None:
            self._fade_old = (self.buf, self.ls, self.le)
        self.buf, self.ls, self.le, self.beats, self.downbeats, self.tm = mix, ls, le, beats, down, tm
        self.render_info = info

    # ------------------------------------------------------------ audio callback
    def _indices(self, start, frames, ls, le, n):
        idx = start + np.arange(frames)
        if self.loop_on and le > ls:
            over = idx >= le
            if over.any():
                idx[over] = ls + (idx[over] - le) % (le - ls)
        return idx

    def _read(self, buf, idx):
        n = len(buf)
        ok = (idx >= 0) & (idx < n)
        out = np.zeros((len(idx), 2), np.float32)
        out[ok] = buf[idx[ok]]
        return out

    def process(self, frames: int) -> np.ndarray:
        """Produce the next `frames` stereo frames (frames, 2) float32: timeline + live voices + click."""
        t0 = time.perf_counter()
        while self.cmds:
            c = self.cmds.popleft()
            if c[0] == "voice":
                self.voices.append(c[1])
            elif c[0] == "off":
                for v in self.voices:
                    if v.track == c[1] and v.key == c[2] and v.fade_from is None:
                        v.release(c[3])
        out = np.zeros((frames, 2), np.float32)
        with self.lock:
            if self._next is not None and self.playing:
                self._swap(crossfade=True)
            buf = self.buf
            if self.playing and buf is not None:
                idx = self._indices(self.pos, frames, self.ls, self.le, len(buf))
                cur = self._read(buf, idx)
                if self._fade_old is not None:
                    ob, ols, ole = self._fade_old
                    old = self._read(ob, self._indices(self.pos, frames, ols, ole, len(ob)))
                    ramp = np.linspace(0.0, 1.0, frames, dtype=np.float32)[:, None]
                    cur = old * (1 - ramp) + cur * ramp
                    self._fade_old = None
                out += cur
                if self.metronome and len(self.beats):
                    hits = np.nonzero(np.isin(idx, self.beats))[0]
                    for o in hits:
                        self.voices.append(Voice(self._clicks[0 if int(idx[o]) in self.downbeats else 1], delay=o, click=True))
                nxt = int(idx[-1]) + 1
                if self.loop_on and self.le > self.ls and nxt >= self.le:
                    nxt = self.ls
                self.pos = nxt
                if not (self.loop_on and self.le > self.ls) and self.pos >= len(buf):
                    self.playing = False
                    self._commit_recording_async()
        if self.voices:
            for v in self.voices:
                out += v.render(frames)
            self.voices = [v for v in self.voices if not v.done]
        _soft_clip(out)
        dt = time.perf_counter() - t0
        self.cpu = 0.9 * self.cpu + 0.1 * (dt / (frames / self.sr))
        return out

    def _cb(self, outdata, frames, time_info, status):
        if status and status.output_underflow:
            self.xruns += 1
        try:
            outdata[:] = self.process(frames)
        except Exception as e:  # noqa: BLE001  never let the audio thread die
            outdata.fill(0)
            self.error = f"audio: {e}"

    # ------------------------------------------------------------ output
    def ensure_output(self):
        if self.output_kind is not None:
            return self.output_kind
        req = self.output_req
        if req == "manual":
            self.output_kind = "manual"
            return self.output_kind
        if req in ("auto", "portaudio"):
            try:
                self._open_stream()
                self.output_kind = "portaudio"
                return self.output_kind
            except Exception as e:  # noqa: BLE001
                self.error = f"no audio output ({type(e).__name__}: {e}); playing silently"
                if req == "portaudio":
                    raise
        self.output_kind = "null"
        self.latency = self.blocksize / self.sr
        self._null_thread = threading.Thread(target=self._null_loop, daemon=True, name="aidaw-null-audio")
        self._null_thread.start()
        return self.output_kind

    def _null_loop(self):
        nxt = time.perf_counter()
        while not self._stop.is_set() and self.output_kind == "null":
            self.process(self.blocksize)
            nxt += self.blocksize / self.sr
            time.sleep(max(0.0, nxt - time.perf_counter()))

    def _open_stream(self):
        sd = _sd()
        dev = self.device
        if isinstance(dev, str) and dev.isdigit():
            dev = int(dev)
        last = None
        rates = [self.sr]
        try:
            d = sd.query_devices(dev, "output")
            if int(d["default_samplerate"]) not in rates:
                rates.append(int(d["default_samplerate"]))
        except Exception:
            pass
        for sr in rates:
            try:
                st = sd.OutputStream(samplerate=sr, blocksize=self.blocksize, device=dev, channels=2,
                                     dtype="float32", callback=self._cb, latency="low")
                if sr != self.sr:
                    self.sr = sr
                    self._clicks = (_click(sr, True), _click(sr, False))
                    self.cache = {}
                    self.invalidate()
                st.start()
                self.stream = st
                self.latency = float(st.latency) + self.blocksize / sr
                return
            except Exception as e:  # noqa: BLE001
                last = e
        raise last

    def close_output(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
        self.stream = None
        self.output_kind = None

    def set_audio(self, device=None, blocksize=None, output=None):
        was = self.output_kind
        self.close_output()
        if device is not None:
            self.device = None if device in ("", "default", None) else device
        if blocksize:
            self.blocksize = int(blocksize)
        if output:
            self.output_req = output
        if was is not None or output:
            self.ensure_output()
        return self.status()

    # ------------------------------------------------------------ transport
    def _qn(self, at):
        if at is None:
            return None
        if isinstance(at, (int, float)):
            return float(at)
        return self.tm.qn(str(at))

    def play(self, at=None):
        self.ensure_output()
        if at is not None:
            self.seek(at)
        self.playing = True
        return self.status()

    def stop(self, rewind=False):
        self.playing = False
        for v in self.voices:
            v.release(int(0.03 * self.sr))
        self._commit_recording()
        if rewind:
            self.pos = self.ls if self.loop_on else 0
        return self.status()

    def seek(self, at):
        q = self._qn(at)
        with self.lock:
            self.pos = max(0, int(round(self.tm.sec(max(0.0, q)) * self.sr)))
            self._fade_old = None
        return self.status()

    def set_loop(self, on=None, start=None, end=None):
        if start is not None and end is not None:
            self.s.apply("loop.set", {"start": start, "end": end}, author="human", reason="loop range from the transport")
            self.tm = TimeMap(self.s.doc["transport"])
            if on is None:
                on = True
        if on is not None:
            on = str(on).lower() not in ("false", "0", "off", "no")
            if on != self.loop_on:
                self.loop_on = on
                self.invalidate()
        return self.status()

    def set_metronome(self, on=True):
        self.metronome = str(on).lower() not in ("false", "0", "off", "no")
        return self.status()

    def heard_sample(self):
        p = self.pos - int(self.latency * self.sr) if self.playing else self.pos
        if self.loop_on and self.le > self.ls and p < self.ls and self.pos >= self.ls:
            p = self.le - (self.ls - p)
        return max(0, p)

    def position_qn(self):
        return self.tm.qn_at_sec(self.heard_sample() / self.sr)

    def status(self):
        q = self.position_qn()
        tm = self.tm
        lp = self.s.doc["transport"].get("loop")
        return {"playing": self.playing, "posQn": round(q, 4), "pos": tm.pos(q), "bpm": tm.bpm_at(q),
                "qnPerSec": tm.bpm_at(q) / 60.0, "time": round(self.heard_sample() / self.sr, 3),
                "loop": self.loop_on, "loopRange": ({"start": tm.qn(lp["start"]), "end": tm.qn(lp["end"])} if lp else None),
                "metronome": self.metronome, "record": self.rec_on, "recordTrack": self.rec_track,
                "recordQuantize": self.rec_quant, "recordPending": len(self.rec_notes) + len(self.rec_pending),
                "render": {"state": self.render_state, **self.render_info},
                "output": self.output_kind or "closed", "device": self.device, "blocksize": self.blocksize,
                "sampleRate": self.sr, "latencyMs": round(self.latency * 1000, 1), "cpu": round(self.cpu * 100, 1),
                "xruns": self.xruns, "midiPort": self.midi_port, "midiTrack": self.midi_track,
                "active": [{"track": t, "key": k, "vel": v} for (t, k), v in list(self.active.items())],
                "error": self.error, "where": "local", "serverTime": time.time()}

    # ------------------------------------------------------------ live notes
    def _track(self, ref):
        ref = (ref or self.midi_track or "")
        for t in self.s.doc["tracks"]:
            if t["id"] == ref or t["name"].lower() == str(ref).lower():
                return t
        inst = [t for t in self.s.doc["tracks"] if t["type"] == "instrument"]
        return inst[0] if inst else None

    def _voice_audio(self, t, key, vel):
        inst = t.get("instrument") or {"plugin": "builtin:synth"}
        fmt, _, name = str(inst.get("plugin", "builtin:synth")).partition(":")
        params = inst.get("params") or {}
        rng = np.random.default_rng(key * 131 + int(vel))
        stand_in = False
        if fmt == "builtin" and name == "drums":
            p = dict(dsp.PARAMS["drums"]); p.update(params)
            y = dsp.drum_hit(key, vel, self.sr, rng, {k: v for k, v in p.items() if not k.startswith("_")})
            rel = None
        else:
            if fmt == "builtin" and name == "synth":
                p = dict(dsp.PARAMS["synth"]); p.update(params)
            else:
                stand_in = fmt != "builtin" or name != "synth"
                p = dict(dsp.PARAMS["synth"]); p.update({"wave": "triangle", "voices": 2, "detune": 6, "cutoff": 3000,
                                                         "attack": 0.004, "decay": 0.4, "sustain": 0.5, "release": 0.25,
                                                         "gainDb": -8})
            p = {k: v for k, v in p.items() if not k.startswith("_")}
            y = dsp.synth_note(p, key, vel, int(3.0 * self.sr), self.sr, rng)
            rel = int(max(0.01, float(p.get("release", 0.15))) * self.sr)
        mix = t.get("mix", {})
        g = float(dsp.db2lin(mix.get("gainDb", 0.0)))
        gl, gr = dsp.pan_gains(mix.get("pan", 0.0))
        out = np.stack([y[0] * g * gl, y[1] * g * gr], 1).astype(np.float32)
        return out, rel, stand_in

    def note_on(self, track=None, key=60, vel=100):
        t = self._track(track)
        if t is None:
            return {"ok": False, "error": "no instrument track"}
        key = note_number(key)
        vel = max(1, min(127, int(vel)))
        self.ensure_output()
        audio, rel, stand_in = self._voice_audio(t, key, vel)
        v = Voice(audio, t["id"], key)
        v.fade_len = rel or 0
        self.cmds.append(("voice", v))
        self.active[(t["id"], key)] = vel
        if self.rec_on and self.playing:
            self.rec_pending[(t["id"], key)] = (self.position_qn(), vel)
            self.rec_track = self.rec_track or t["id"]
        return {"ok": True, "track": t["id"], "key": key, "standIn": stand_in}

    def note_off(self, track=None, key=60):
        t = self._track(track)
        if t is None:
            return {"ok": False}
        key = note_number(key)
        self.active.pop((t["id"], key), None)
        rel = int(0.15 * self.sr)
        inst = (t.get("instrument") or {})
        if str(inst.get("plugin", "")).startswith("builtin:synth"):
            rel = int(max(0.01, float((inst.get("params") or {}).get("release", 0.15))) * self.sr)
        self.cmds.append(("off", t["id"], key, rel))
        st = self.rec_pending.pop((t["id"], key), None)
        if st is not None:
            q0, vel = st
            q1 = self.position_qn()
            if q1 < q0:                                 # wrapped around the loop
                q1 = q0 + 0.25
            self.rec_notes.append((t["id"], q0, q1, key, vel))
        return {"ok": True}

    def midi_message(self, msg):
        """A mido-style message (type, note, velocity, channel) from a keyboard."""
        typ = getattr(msg, "type", None)
        if typ == "note_on" and getattr(msg, "velocity", 0) > 0:
            self.note_on(self.midi_track, msg.note, msg.velocity)
        elif typ in ("note_off", "note_on"):
            self.note_off(self.midi_track, msg.note)

    # ------------------------------------------------------------ MIDI ports
    @staticmethod
    def midi_ports():
        try:
            import mido
            return {"inputs": mido.get_input_names(), "available": True}
        except Exception as e:  # noqa: BLE001
            return {"inputs": [], "available": False,
                    "error": f"MIDI input needs python-rtmidi ({type(e).__name__}: {e})"}

    def midi_open(self, port=None, track=None):
        import mido
        self.midi_close()
        names = mido.get_input_names()
        if not names:
            raise RuntimeError("no MIDI input ports found")
        name = port if port in names else next((n for n in names if port and str(port).lower() in n.lower()), names[0])
        self.midi_in = mido.open_input(name, callback=self.midi_message)
        self.midi_port = name
        if track:
            self.midi_track = self._track(track)["id"]
        return self.status()

    def midi_close(self):
        if self.midi_in is not None:
            try:
                self.midi_in.close()
            except Exception:
                pass
        self.midi_in = None
        self.midi_port = None
        return self.status()

    # ------------------------------------------------------------ recording
    def set_record(self, on=True, track=None, quantize=None):
        on = str(on).lower() not in ("false", "0", "off", "no")
        if track:
            t = self._track(track)
            self.rec_track = t["id"] if t else None
            self.midi_track = self.rec_track
        if quantize is not None:
            self.rec_quant = float(quantize)
        if not on and self.rec_on:
            self._commit_recording()
        self.rec_on = on
        return self.status()

    def _commit_recording_async(self):
        if self.rec_notes or self.rec_pending:
            threading.Thread(target=self._commit_recording, daemon=True).start()

    def _commit_recording(self):
        """Write recorded notes into the pattern under each note (one undo step)."""
        q1 = self.position_qn()
        for (tid, key), (q0, vel) in list(self.rec_pending.items()):
            self.rec_notes.append((tid, q0, max(q1, q0 + 0.25), key, vel))
        self.rec_pending.clear()
        notes, self.rec_notes = self.rec_notes, []
        if not notes:
            return None
        from .render import clip_len_qn
        doc = self.s.doc
        tm = TimeMap(doc["transport"])
        g = self.rec_quant
        by_pat: dict = {}
        new_ops = []
        made = {}
        for tid, q0, q1, key, vel in notes:
            t = next((x for x in doc["tracks"] if x["id"] == tid), None)
            if t is None:
                continue
            clip = None
            for c in t.get("clips", []):
                if "pattern" not in c:
                    continue
                c0 = tm.qn(c["at"]); c1 = c0 + clip_len_qn(doc, tm, c)
                if c0 - 1e-6 <= q0 < c1:
                    clip = (c, c0)
                    break
            if clip is None:                       # no pattern there: make one for the bar(s)
                bar = tm.bars_to_qn(1)
                b0 = float(np.floor(q0 / bar) * bar)
                pid = made.get((tid, b0))
                if pid is None:
                    pid = f"{tid}_rec{len(doc.get('patterns', {})) + len(made) + 1}"
                    made[(tid, b0)] = pid
                    new_ops.append({"op": "pattern.add", "args": {"id": pid, "lengthBeats": bar * 2, "notes": []}})
                    new_ops.append({"op": "clip.add_pattern", "args": {"track": tid, "pattern": pid, "at": tm.pos(b0)}})
                rel, L = q0 - b0, bar * 2
            else:
                c, c0 = clip
                pid = c["pattern"]
                L = float(doc["patterns"][pid]["lengthBeats"])
                rel = (q0 - c0) % L
            ln = max(0.05, q1 - q0)
            if g and g > 0:
                rel = round(rel / g) * g
                ln = max(g, round(ln / g) * g)
                if rel >= L:
                    rel -= L
            by_pat.setdefault(pid, []).append({"t": round(rel, 4), "len": round(ln, 4), "key": int(key), "vel": int(vel)})
        ops = new_ops + [{"op": "pattern.add_notes", "args": {"pattern": pid, "notes": ns}} for pid, ns in by_pat.items()]
        if not ops:
            return None
        n = sum(len(v) for v in by_pat.values())
        return self.s.apply("batch", {"ops": ops}, author="human", label="record",
                            reason=f"recorded {n} note{'s' * (n != 1)} from live input"
                                   + (f", quantized to {g:g} beats" if g else ""))

    def close(self):
        self._stop.set()
        self._dirty.set()
        self.midi_close()
        self.close_output()
        self.output_kind = "closed"


# ---------------------------------------------------------------- session glue
def engine_for(session, create=True, output=None) -> Engine | None:
    e = ENGINES.get(id(session))
    if e is not None and (e.s is not session or e.output_kind == "closed"):
        e = None
    if e is None and create:
        e = Engine(session, output=output or os.environ.get("PLAINDAW_AUDIO_OUTPUT", "auto"))
        ENGINES[id(session)] = e
    return e


def server_file(session) -> Path:
    return session.dir / "renders" / SERVER_FILE


def write_server_file(session, host, port):
    import json
    f = server_file(session)
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps({"pid": os.getpid(), "host": host, "port": port}), encoding="utf-8")
    session._serving = True


def remove_server_file(session):
    try:
        f = server_file(session)
        import json
        if f.exists() and json.loads(f.read_text()).get("pid") == os.getpid():
            f.unlink()
    except Exception:
        pass


def _remote(session):
    """A running GUI server for this project (another process): forward live calls there."""
    if getattr(session, "_serving", False) or os.environ.get("PLAINDAW_LIVE_LOCAL"):
        return None
    f = server_file(session)
    if not f.exists():
        return None
    try:
        import json
        info = json.loads(f.read_text(encoding="utf-8"))
        if info.get("pid") == os.getpid():
            return None
        return f"http://{info.get('host', '127.0.0.1')}:{int(info['port'])}/rpc"
    except Exception:
        return None


def _forward(url, method, params):
    import json
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        j = json.loads(r.read().decode("utf-8"))
    if "error" in j:
        raise RuntimeError(j["error"]["message"])
    res = j["result"]
    if isinstance(res, dict):
        res["where"] = "server"
    return res


def _live(method, fn):
    def q(s, p, author):
        url = _remote(s)
        if url:
            try:
                return _forward(url, method, {k: v for k, v in p.items() if not k.startswith("_")})
            except OSError:
                pass        # server gone: run here
        return fn(engine_for(s), p)
    return q


def _b(v, default=True):
    if v is None:
        return default
    return str(v).lower() not in ("false", "0", "off", "no")


def _devices(e, p):
    try:
        sd = _sd()
        apis = sd.query_hostapis()
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                out.append({"id": i, "name": d["name"], "hostapi": apis[d["hostapi"]]["name"],
                            "outputs": d["max_output_channels"], "defaultSampleRate": d["default_samplerate"],
                            "lowLatencyMs": round(d["default_low_output_latency"] * 1000, 1)})
        try:
            default = sd.default.device[1]
        except Exception:
            default = None
        return {"devices": out, "default": default, "hostapis": [a["name"] for a in apis],
                "asio": bool(os.environ.get("SD_ENABLE_ASIO")), "current": e.device}
    except Exception as ex:  # noqa: BLE001
        return {"devices": [], "error": f"no PortAudio ({type(ex).__name__}: {ex})", "current": e.device}


LIVE_QUERIES = {
    "transport.play": lambda e, p: e.play(p.get("at")),
    "transport.stop": lambda e, p: e.stop(rewind=_b(p.get("rewind"), False)),
    "transport.seek": lambda e, p: e.seek(p.get("at", 0)),
    "transport.loop": lambda e, p: e.set_loop(p.get("on"), p.get("start"), p.get("end")),
    "transport.metronome": lambda e, p: e.set_metronome(p.get("on", True)),
    "transport.record": lambda e, p: e.set_record(p.get("on", True), p.get("track"), p.get("quantize")),
    "transport.status": lambda e, p: e.status(),
    "audio.devices": _devices,
    "audio.set": lambda e, p: e.set_audio(p.get("device"), p.get("blocksize"), p.get("output")),
    "midi.ports": lambda e, p: e.midi_ports(),
    "midi.open": lambda e, p: e.midi_open(p.get("port"), p.get("track")),
    "midi.close": lambda e, p: e.midi_close(),
    "midi.target": lambda e, p: (setattr(e, "midi_track", (e._track(p.get("track")) or {}).get("id")), e.status())[1],
    "live.note": lambda e, p: (e.note_on(p.get("track"), p.get("key", 60), p.get("vel", 100))
                               if _b(p.get("on"), True) else e.note_off(p.get("track"), p.get("key", 60))),
}
LIVE_HELP = {
    "transport.play": "Live playback of the timeline {at?}", "transport.stop": "Stop {rewind?}",
    "transport.seek": "Move the playhead {at}", "transport.loop": "Loop on/off {on, start?, end?}",
    "transport.metronome": "Click on/off {on}", "transport.record": "Record live notes into patterns {on, track, quantize}",
    "transport.status": "Playhead, render state, latency, CPU, MIDI", "audio.devices": "Audio outputs (WASAPI/ASIO/...)",
    "audio.set": "Choose output {device, blocksize, output: auto|null}", "midi.ports": "MIDI inputs",
    "midi.open": "Play a track live from a MIDI keyboard {port, track}", "midi.close": "Close the MIDI input",
    "midi.target": "Which track the keyboard plays {track}", "live.note": "Sound a note now {track, key, vel, on}",
}
LIVE_POSITIONAL = {"transport.play": ["at"], "transport.seek": ["at"], "transport.loop": ["on"],
                   "transport.metronome": ["on"], "transport.record": ["on"], "audio.set": ["device"],
                   "midi.open": ["port"], "midi.target": ["track"], "live.note": ["track", "key"]}


def register(QUERIES, METHODS_DOC, QUERY_POSITIONAL):
    for k, fn in LIVE_QUERIES.items():
        QUERIES[k] = _live(k, fn)
    METHODS_DOC.update(LIVE_HELP)
    QUERY_POSITIONAL.update(LIVE_POSITIONAL)
