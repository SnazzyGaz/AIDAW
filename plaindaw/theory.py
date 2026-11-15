"""Minimal music theory the musical commands need: keys, scales, chords, progressions,
and genre presets (tempo, form, drum/bass styles). Plain data, no external sources."""
from __future__ import annotations

import re

PC = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "F": 5, "F#": 6, "GB": 6, "G": 7,
      "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11, "CB": 11, "E#": 5, "FB": 4, "B#": 0}
NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10], "phrygian": [0, 1, 3, 5, 7, 8, 10], "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10], "locrian": [0, 1, 3, 5, 6, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11], "melodic_minor": [0, 2, 3, 5, 7, 9, 11],
    "major_pentatonic": [0, 2, 4, 7, 9], "minor_pentatonic": [0, 3, 5, 7, 10], "blues": [0, 3, 5, 6, 7, 10],
    "chromatic": list(range(12)),
}
SCALE_ALIASES = {"maj": "major", "ionian": "major", "min": "minor", "m": "minor", "aeolian": "minor",
                 "harmonic minor": "harmonic_minor", "melodic minor": "melodic_minor",
                 "pentatonic": "minor_pentatonic", "minor pentatonic": "minor_pentatonic",
                 "major pentatonic": "major_pentatonic"}

CHORDS = {  # quality -> intervals
    "": [0, 4, 7], "maj": [0, 4, 7], "m": [0, 3, 7], "min": [0, 3, 7], "dim": [0, 3, 6], "aug": [0, 4, 8],
    "sus2": [0, 2, 7], "sus4": [0, 5, 7], "5": [0, 7], "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11],
    "m7": [0, 3, 7, 10], "min7": [0, 3, 7, 10], "dim7": [0, 3, 6, 9], "m7b5": [0, 3, 6, 10],
    "add9": [0, 4, 7, 14], "madd9": [0, 3, 7, 14], "6": [0, 4, 7, 9], "m6": [0, 3, 7, 9], "9": [0, 4, 7, 10, 14],
    "m9": [0, 3, 7, 10, 14],
}

ROMAN = {"I": 0, "II": 1, "III": 2, "IV": 3, "V": 4, "VI": 5, "VII": 6}


class TheoryError(ValueError):
    pass


def pc_of(name: str) -> int:
    n = name.strip().upper().replace("♯", "#").replace("♭", "B")
    if n not in PC:
        raise TheoryError(f"unknown note name {name!r}")
    return PC[n]


def parse_key(text: str) -> tuple[int, str]:
    """'F minor', 'Fm', 'F# dorian', 'Bb', 'A min' -> (tonic pc, scale name)."""
    t = str(text).strip()
    m = re.match(r"^([A-Ga-g])([#b♯♭]?)\s*(.*)$", t)
    if not m:
        raise TheoryError(f"bad key {text!r} (try 'F minor' or 'Fm')")
    tonic = pc_of(m.group(1) + m.group(2))
    rest = m.group(3).strip().lower()
    if rest in ("", "maj", "major", "ionian"):
        scale = "major"
    else:
        scale = SCALE_ALIASES.get(rest, rest.replace(" ", "_"))
    if scale not in SCALES:
        raise TheoryError(f"unknown scale {rest!r} (known: {', '.join(SCALES)})")
    return tonic, scale


def key_name(tonic: int, scale: str) -> str:
    names = NAMES_FLAT if scale in ("minor", "dorian", "phrygian", "locrian") and tonic in (3, 5, 8, 10, 0, 7) else NAMES_SHARP
    return f"{names[tonic % 12]} {scale.replace('_', ' ')}"


def scale_pcs(tonic: int, scale: str) -> list[int]:
    return [(tonic + i) % 12 for i in SCALES[scale]]


def parse_chord(sym: str) -> tuple[int, list[int], str]:
    """'Fm', 'Db', 'C#m7', 'Gsus4', 'Ab/C' -> (root pc, intervals, quality)."""
    s = sym.strip().split("/")[0]
    m = re.match(r"^([A-Ga-g])([#b♯♭]?)(.*)$", s)
    if not m:
        raise TheoryError(f"bad chord {sym!r}")
    root = pc_of(m.group(1) + m.group(2))
    q = m.group(3)
    q = {"M": "maj", "-": "m", "mi": "m", "maj": "maj", "major": "maj", "minor": "m"}.get(q, q)
    if q not in CHORDS:
        raise TheoryError(f"unknown chord quality {q!r} in {sym!r} (known: {', '.join(k for k in CHORDS if k)})")
    return root, CHORDS[q], q


def roman_chord(numeral: str, tonic: int, scale: str) -> tuple[int, list[int], str]:
    """Roman numeral in key -> chord. Upper = major, lower = minor, ° = dim, '7' adds a 7th from the scale."""
    m = re.match(r"^(b|#)?([ivIV]+)(°|o|dim)?(7|maj7|M7|Δ7?|sus2|sus4|add9|9)?$", numeral.strip())
    if not m:
        raise TheoryError(f"bad numeral {numeral!r}")
    deg = ROMAN.get(m.group(2).upper())
    if deg is None:
        raise TheoryError(f"bad numeral {numeral!r}")
    steps = SCALES[scale] if len(SCALES[scale]) == 7 else SCALES["minor" if scale.startswith("minor") else "major"]
    root = (tonic + steps[deg] + (-1 if m.group(1) == "b" else 1 if m.group(1) == "#" else 0)) % 12
    if m.group(3):
        q = "dim"
    elif m.group(2).isupper():
        q = "maj"
    else:
        q = "m"
    ext = m.group(4)
    if ext == "7":
        q = {"maj": "7", "m": "m7", "dim": "m7b5"}[q]
    elif ext in ("maj7", "M7", "Δ", "Δ7"):
        q = "maj7" if q == "maj" else "mmaj7" if "mmaj7" in CHORDS else "m7"
    elif ext in ("sus2", "sus4"):
        q = ext
    elif ext in ("add9", "9"):
        q = {"maj": "add9", "m": "madd9", "dim": "dim"}[q] if "add9" in CHORDS and "madd9" in CHORDS else q
    return root, CHORDS[q], q


def diatonic_chord(degree: int, tonic: int, scale: str, sevenths=False) -> tuple[int, list[int], str]:
    steps = SCALES[scale] if len(SCALES[scale]) == 7 else SCALES["minor"]
    pcs = [(tonic + steps[(degree + k) % 7] + 12 * ((degree + k) // 7)) for k in (0, 2, 4, 6)]
    root = pcs[0] % 12
    ivs = [p - pcs[0] for p in pcs[: 4 if sevenths else 3]]
    q = {(0, 4, 7): "maj", (0, 3, 7): "m", (0, 3, 6): "dim", (0, 4, 8): "aug"}.get(tuple(ivs[:3]), "")
    return root, ivs, q


def parse_progression(text: str, tonic: int, scale: str) -> list[tuple[int, list[int], str]]:
    """'Fm Db Ab Eb' or 'i VI III VII' (space/comma/dash separated)."""
    out = []
    for tok in re.split(r"[\s,|\-]+", text.strip()):
        if not tok:
            continue
        if re.match(r"^(b|#)?[ivIV]+(°|o|dim)?(7|maj7|M7|Δ7?|sus2|sus4|add9|9)?$", tok):
            out.append(roman_chord(tok, tonic, scale))
        else:
            out.append(parse_chord(tok))
    if not out:
        raise TheoryError("empty chord progression")
    return out


def chord_symbol(root: int, quality: str, flats=True) -> str:
    names = NAMES_FLAT if flats else NAMES_SHARP
    return names[root % 12] + ("" if quality in ("", "maj") else quality)


def voice_lead(chords: list[tuple[int, list[int], str]], center: int = 60, voices: int = 4) -> list[list[int]]:
    """Choose inversions near `center` that move as little as possible."""
    out, prev = [], None
    for root, ivs, _ in chords:
        pcs = sorted({(root + i) % 12 for i in ivs})
        best, best_cost = None, None
        for base in range(center - 12, center + 1):
            # build a voicing: lowest chord tone >= base, then stack upward
            notes = []
            n = base
            while len(notes) < voices:
                if n % 12 in pcs and (not notes or n > notes[-1]):
                    notes.append(n)
                n += 1
                if n > base + 30:
                    break
            if len(notes) < min(voices, 3):
                continue
            if (root % 12) not in {x % 12 for x in notes}:
                continue
            cost = abs(sum(notes) / len(notes) - center) * 0.5
            if prev:
                cost += sum(min(abs(a - b) for b in prev) for a in notes)
            if best_cost is None or cost < best_cost:
                best, best_cost = notes, cost
        out.append(best)
        prev = best
    return out


# ------------------------------------------------------------------ genre presets
# Drum steps are 16th-note indices within one 4/4 bar (0..15). Keys are GM drum notes.
GENRES = {
    "bigroom": {"bpm": 128, "key": "F minor", "progression": "i VI III VII", "bass": "offbeat",
                "chords": "supersaw", "lead": "anthem", "swing": 0.0,
                "drums": {"kick": [0, 4, 8, 12], "clap": [4, 12], "hat": [2, 6, 10, 14], "openhat": []},
                "form": "intro:8,build:8,drop:16,break:8,build:8,drop:16,outro:8"},
    "house": {"bpm": 124, "key": "A minor", "progression": "i VII VI VII", "bass": "offbeat",
              "chords": "stabs", "lead": "pluck", "swing": 0.08,
              "drums": {"kick": [0, 4, 8, 12], "clap": [4, 12], "hat": [2, 6, 10, 14], "openhat": [2, 6, 10, 14],
                        "shaker": list(range(16))},
              "form": "intro:16,groove:16,break:8,drop:16,outro:16"},
    "techno": {"bpm": 132, "key": "A minor", "progression": "i i VI VII", "bass": "rolling",
               "chords": "stabs", "lead": "arp", "swing": 0.0,
               "drums": {"kick": [0, 4, 8, 12], "clap": [4, 12], "hat": [2, 6, 10, 14], "openhat": [],
                         "ride": [0, 2, 4, 6, 8, 10, 12, 14]},
               "form": "intro:16,groove:32,break:16,drop:32,outro:16"},
    "dnb": {"bpm": 174, "key": "E minor", "progression": "i VI VII v", "bass": "reese",
            "chords": "pad", "lead": "pluck", "swing": 0.0,
            "drums": {"kick": [0, 10], "snare": [4, 12], "hat": [2, 6, 8, 14], "openhat": []},
            "form": "intro:16,build:8,drop:32,break:16,drop:32,outro:8"},
    "trap": {"bpm": 140, "key": "C minor", "progression": "i VI iv V", "bass": "808",
             "chords": "pad", "lead": "bell", "swing": 0.0,
             "drums": {"kick": [0, 7, 10], "clap": [8], "hat": list(range(16)), "openhat": []},
             "form": "intro:8,hook:16,verse:16,hook:16,outro:8"},
    "hiphop": {"bpm": 90, "key": "D minor", "progression": "i iv VII III", "bass": "root",
               "chords": "keys", "lead": "pluck", "swing": 0.15,
               "drums": {"kick": [0, 7, 10], "snare": [4, 12], "hat": [0, 2, 4, 6, 8, 10, 12, 14], "openhat": []},
               "form": "intro:4,verse:16,hook:8,verse:16,hook:8,outro:4"},
    "lofi": {"bpm": 80, "key": "C major", "progression": "IVmaj7 iii7 ii7 Imaj7", "bass": "root",
             "chords": "keys", "lead": "pluck", "swing": 0.18,
             "drums": {"kick": [0, 10], "snare": [4, 12], "hat": [0, 2, 4, 6, 8, 10, 12, 14], "openhat": []},
             "form": "intro:4,a:16,b:16,a:16,outro:4"},
    "ambient": {"bpm": 70, "key": "D minor", "progression": "i VI III VII", "bass": "drone",
                "chords": "pad", "lead": "sparse", "swing": 0.0,
                "drums": {"kick": [], "clap": [], "hat": [], "openhat": []},
                "form": "intro:8,a:16,b:16,outro:8"},
    "horror": {"bpm": 60, "key": "C phrygian", "progression": "i bII i bII", "bass": "drone",
               "chords": "pad", "lead": "sparse", "swing": 0.0,
               "drums": {"kick": [0], "clap": [], "hat": [], "openhat": []},
               "form": "intro:8,tension:16,climax:8,outro:8"},
}
GENRE_ALIASES = {"big room": "bigroom", "edm": "bigroom", "drum and bass": "dnb", "dnb": "dnb", "d&b": "dnb",
                 "hip hop": "hiphop", "hip-hop": "hiphop", "lo-fi": "lofi", "cinematic": "ambient"}
GM = {"kick": 36, "snare": 38, "clap": 39, "hat": 42, "openhat": 46, "ride": 51, "shaker": 44, "crash": 49, "rim": 37}

# Which instrument roles play in each kind of section (used by `arrange`).
SECTION_ROLES = {
    "intro": {"kick", "hat", "pad", "fx"},
    "build": {"pad", "roll", "riser", "hat", "chords", "fx"},
    "drop": {"kick", "clap", "snare", "hat", "openhat", "bass", "chords", "lead", "fx", "shaker", "ride", "crash"},
    "groove": {"kick", "clap", "snare", "hat", "openhat", "bass", "chords", "shaker", "ride"},
    "hook": {"kick", "clap", "snare", "hat", "bass", "chords", "lead"},
    "verse": {"kick", "snare", "clap", "hat", "bass", "pad"},
    "break": {"pad", "chords", "lead", "fx"},
    "tension": {"pad", "bass", "riser", "fx"},
    "climax": {"kick", "pad", "bass", "chords", "lead", "fx"},
    "a": {"kick", "snare", "hat", "bass", "chords", "pad"},
    "b": {"kick", "snare", "hat", "bass", "chords", "pad", "lead"},
    "outro": {"pad", "hat", "fx"},
}


def genre(name: str | None) -> dict:
    if not name:
        return GENRES["bigroom"] | {"name": "bigroom"}
    n = GENRE_ALIASES.get(name.lower(), name.lower())
    if n not in GENRES:
        raise TheoryError(f"unknown style {name!r} (known: {', '.join(GENRES)})")
    return GENRES[n] | {"name": n}


def parse_form(text: str) -> list[tuple[str, int]]:
    """'intro:8,build:8,drop:16' -> [('intro', 8), ...] (bars)."""
    out = []
    for tok in re.split(r"[\s,]+", text.strip()):
        if not tok:
            continue
        name, _, bars = tok.partition(":")
        try:
            b = int(bars or 8)
        except ValueError:
            raise TheoryError(f"bad section {tok!r} (use name:bars)")
        if b <= 0 or b > 512:
            raise TheoryError(f"bad bar count in {tok!r}")
        out.append((name.lower(), b))
    if not out:
        raise TheoryError("empty form")
    return out


def section_kind(name: str) -> str:
    n = re.sub(r"\d+$", "", name.lower()).strip("_- ")
    if n in SECTION_ROLES:
        return n
    for k in SECTION_ROLES:
        if n.startswith(k):
            return k
    return "groove"
