"""Standard MIDI File import/export (for FL Studio and other DAWs)."""
from __future__ import annotations

from pathlib import Path

import mido

from .timebase import TimeMap

PPQ = 960


def export_midi(doc: dict, path: Path, tracks: list | None = None) -> dict:
    tm = TimeMap(doc["transport"])
    mf = mido.MidiFile(type=1, ticks_per_beat=PPQ)
    meta = mido.MidiTrack(); mf.tracks.append(meta)
    ev = []
    for t in doc["transport"]["tempo"]:
        ev.append((tm.qn(t["at"]), mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(t["bpm"]), time=0)))
    for s in doc["transport"].get("timeSig", []):
        ev.append((tm.qn(s["at"]), mido.MetaMessage("time_signature", numerator=s["num"], denominator=s["den"], time=0)))
    for m in doc.get("markers", []):
        ev.append((tm.qn(m["at"]), mido.MetaMessage("marker", text=m["name"], time=0)))
    _write(meta, ev)
    count = 0
    ch = 0
    for t in doc["tracks"]:
        if t["type"] != "instrument" or (tracks and t["id"] not in tracks):
            continue
        mt = mido.MidiTrack(); mf.tracks.append(mt)
        mt.append(mido.MetaMessage("track_name", name=t["name"], time=0))
        channel = 9 if t.get("instrument", {}).get("plugin") == "builtin:drums" else ch % 16
        if channel != 9:
            ch += 1
            if ch % 16 == 9:
                ch += 1
        ev = []
        for c in t.get("clips", []):
            if "pattern" not in c or c.get("mute"):
                continue
            p = doc["patterns"][c["pattern"]]
            L = p["lengthBeats"]
            total = c.get("lengthBeats") or L * c.get("repeat", 1)
            s = tm.qn(c["at"])
            r = 0
            while r * L < total - 1e-9:
                for n in p["notes"]:
                    on = r * L + n["t"]
                    if on >= total - 1e-9:
                        continue
                    off = min(on + n["len"], total)
                    k = max(0, min(127, n["key"] + c.get("transpose", 0)))
                    v = max(1, min(127, int(round(n.get("vel", 100) * c.get("velocity", 100) / 100))))
                    ev.append((s + on, mido.Message("note_on", note=k, velocity=v, channel=channel, time=0)))
                    ev.append((s + off, mido.Message("note_off", note=k, velocity=0, channel=channel, time=0)))
                    count += 1
                r += 1
        _write(mt, ev)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    mf.save(str(path))
    return {"path": str(path), "tracks": len(mf.tracks) - 1, "notes": count}


def _write(track, ev):
    # note_off before note_on at the same tick
    ev.sort(key=lambda e: (e[0], 0 if e[1].type in ("note_off", "set_tempo", "time_signature") else 1))
    last = 0
    for q, msg in ev:
        tick = int(round(q * PPQ))
        track.append(msg.copy(time=tick - last))
        last = tick
    track.append(mido.MetaMessage("end_of_track", time=0))


def read_midi(path: Path) -> dict:
    """Returns {tempo: [(qn,bpm)], timeSig: [(qn,num,den)], markers, tracks: [{name, channel, notes:[{t,len,key,vel}]}]}"""
    mf = mido.MidiFile(str(path))
    ppq = mf.ticks_per_beat
    out = {"tempo": [], "timeSig": [], "markers": [], "tracks": []}
    for tr in mf.tracks:
        tick = 0
        name = None
        open_notes = {}
        notes = []
        chans = set()
        for msg in tr:
            tick += msg.time
            q = tick / ppq
            if msg.type == "set_tempo":
                out["tempo"].append((q, round(mido.tempo2bpm(msg.tempo), 4)))
            elif msg.type == "time_signature":
                out["timeSig"].append((q, msg.numerator, msg.denominator))
            elif msg.type == "marker":
                out["markers"].append((q, msg.text))
            elif msg.type == "track_name":
                name = msg.name
            elif msg.type == "note_on" and msg.velocity > 0:
                open_notes.setdefault((msg.channel, msg.note), []).append((q, msg.velocity))
                chans.add(msg.channel)
            elif msg.type in ("note_off", "note_on"):
                lst = open_notes.get((msg.channel, msg.note))
                if lst:
                    s, v = lst.pop(0)
                    notes.append({"t": round(s, 6), "len": round(max(q - s, 1 / 64), 6), "key": msg.note, "vel": v})
        if notes:
            notes.sort(key=lambda n: (n["t"], n["key"]))
            out["tracks"].append({"name": name or f"MIDI {len(out['tracks']) + 1}", "channel": min(chans) if chans else 0,
                                  "notes": notes})
    return out
