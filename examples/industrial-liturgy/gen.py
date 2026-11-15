# Generates build_industrial.pdaw (built-in layers) and horror_engine.pdaw (the Horror Engine VST3 layer, PC only):
# an 8-bar looping industrial groove in E Phrygian at 84 BPM, written as AIDAW commands.
# Every sound is synthesised by AIDAW's built-in instruments (foundry, bell, synth) or the Horror Engine plugin.
import json, os, random
random.seed(1984)
HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
def nm(k): return f"{NAMES[k%12]}{k//12-1}"
def notes(lst): return " ".join(f"{nm(k)}@{round(t,4)}:{round(l,4)}:{int(max(1,min(127,v)))}" for k,t,l,v in lst)
BAR = 4.0; BARS = 8; BPM = 84
SWING = 0.30     # odd 16ths pushed late by 30% of a 16th (~57% swing): a heavy, lurching machine
def at(bar, step, jitter=0.012):
    """Beat position of a 16th step with machine swing and a little mechanical unevenness."""
    t = bar*BAR + step*0.25 + (0.25*SWING if step % 2 else 0.0)
    return max(0.0, t + random.uniform(-jitter, jitter))
# foundry keys
KICK, PISTON, ANVIL, SLAM, HISS, RATCHET, GEAR, CHAIN, STEAM, SCRAPE, SHEET = 36,37,38,39,40,41,42,43,44,45,46
E1, F1, E2, B2, F2, C3, E3, B3, G3, F3, C4 = 28, 29, 40, 47, 41, 48, 52, 59, 55, 53, 60

kick, piston, metal, gears, grind, bass = [], [], [], [], [], []
for b in range(BARS):
    last = b == BARS-1
    ks = [0, 10] if b % 2 == 0 else [0, 7, 10]
    if b == 3: ks = [0, 7, 10, 13]
    if last: ks = [0, 3, 10]
    for st in ks: kick.append((KICK, at(b, st, 0.006), 0.5, 124 if st == 0 else 108))
    # pistons on the off-8ths, the machine's pulse; the hydraulic release hisses after the 2-bar cycle
    for st in (2, 6, 14) if not last else (2, 6):
        piston.append((PISTON, at(b, st), 0.3, 104 if st == 6 else 92 + random.randint(-6, 4)))
    if b % 2 == 1: piston.append((HISS, at(b, 11), 1.2, 84))
    if last: piston.append((STEAM, at(b, 12), 1.0, 96))
    # backbeat: anvil + metal slam on beat 3, ghost anvil strikes
    metal.append((SLAM, at(b, 8, 0.004), 0.5, 120)); metal.append((ANVIL, at(b, 8, 0.004), 0.5, 100))
    if b % 2 == 1: metal.append((ANVIL, at(b, 15), 0.3, 64))
    if b in (3, 7): metal.append((SHEET, at(b, 12), 1.0, 90))
    # gears: every 16th, accents every 3 steps (3-against-4 turning against the bar), ratchet before the backbeat
    for st in range(16):
        acc = 96 if (b*16+st) % 3 == 0 else 60
        gears.append((GEAR, at(b, st, 0.01), 0.1, acc + random.randint(-8, 8)))
    if b % 2 == 0: gears.append((RATCHET, at(b, 5), 0.4, 92))
    if b in (3, 7): gears.append((CHAIN, at(b, 4), 0.8, 88))
    # grinding motor scrape, long, on alternate bars
    if b in (1, 5): grind.append((SCRAPE, at(b, 0), 3.0, 82))
    if b in (3, 7): grind.append((SCRAPE, at(b, 8), 2.0, 70))
    # bass pulse locked to kick + pistons; the Phrygian b2 (F) at the turnarounds
    hits = sorted(set(ks + [2, 6, 14]))
    if last: hits = sorted(set(ks + [2, 6]))
    for st in hits:
        k = E1
        if (b == 3 and st >= 13) or (last and st >= 6): k = F1
        if st == 14 and b % 2 == 1 and b != 3: k = E2
        bass.append((k, at(b, st, 0.004), 0.22 if st in (2, 6, 14) else 0.45, 118 if st in ks else 96))

# motor drone: one long E1/E2 note, the LFO turns it twice a bar
drone = [(E1, 0.0, BARS*BAR - 0.05, 100), (E2, 0.0, BARS*BAR - 0.05, 80)]
# choir "ahh": E3 + B3 (open fifth) held, colour shifts to the b2 (F) in bar 8 only on the lower voice
choir = [(E3, 0.0, 7*BAR, 80), (B3, 0.0, BARS*BAR - 0.1, 70), (F3, 7*BAR, BAR - 0.1, 76)]
# deep tolling bell, every 2 bars, the last one answered in the b2
bell = [(E3, b*BAR, 3.0, 110) for b in (0, 2, 4, 6)] + [(F3, 7*BAR + 2.0, 2.0, 92)]
# sparse modal organ fifths
organ = [(E2, 2*BAR, 6.0, 80), (B2, 2*BAR, 6.0, 74),
         (F2, 6*BAR, 3.5, 80), (C3, 6*BAR, 3.5, 74), (E2, 6*BAR + 4.0, 3.9, 78), (B2, 6*BAR + 4.0, 3.9, 72)]

L = []; w = L.append
def track(name, tid, plugin, params, role, gain, pan=None):
    w(f'track add "{name}" --id {tid} --plugin {plugin} --role {role}' + (f' --params "{params}"' if params else ""))
    w(f"mix set {tid} --gain {gain}" + (f" --pan {pan}" if pan is not None else ""))
def pat(tid, ns):
    w(f'pattern add {tid}_p --lengthBeats {BARS*BAR:g} --notes "{notes(sorted(ns, key=lambda n: n[1]))}"')
    w(f"clip add_pattern {tid} {tid}_p 1.1.1")
def fx(tid, kind, iid, params): w(f'plugin add {tid} builtin:{kind} --id {iid} --params "{params}"')

w("# AIDAW Industrial Liturgy: heavy machinery groove, E Phrygian, 84 BPM, 8 bars, loop on. All sounds synthesised.")
w('project set --title "Industrial Liturgy" --author "Garyck Arntzen"')
w(f"tempo set --bpm {BPM}")
w('keysig set --key "E phrygian"')
w(f"loop set 1.1.1 {BARS+1}.1.1")
w("# --- machine groove (built-in foundry kit, swung 16ths with mechanical jitter)")
track("Sub Kick", "kick", "builtin:foundry", "kickPitch=41.2 kickDecay=0.75 drive=9 gainDb=-2", "kick", -3)
pat("kick", kick)
fx("kick", "eq", "kick_eq", "lowCutHz=28 peak1Hz=300 peak1Db=-3")
track("Pistons", "piston", "builtin:foundry", "drive=7 size=1.1 gainDb=-4", "perc", -8, -0.15)
pat("piston", piston)
fx("piston", "eq", "piston_eq", "lowCutHz=70")
track("Anvil", "metal", "builtin:foundry", "drive=5 size=0.9 tune=-0.9 gainDb=-4", "clap", -9, 0.1)
pat("metal", metal)
fx("metal", "eq", "metal_eq", "lowCutHz=150 peak1Hz=3500 peak1Db=-2")
track("Gears", "gears", "builtin:foundry", "ratchetClicks=10 ratchetGap=0.024 chainLinks=26 gainDb=-6", "hat", -15, 0.3)
pat("gears", gears)
fx("gears", "eq", "gears_eq", "lowCutHz=900 highCutHz=11000")
fx("gears", "bitcrush", "gears_crush", "bits=7 downsample=3 mix=0.45")
track("Grinder", "grind", "builtin:foundry", "drive=8 size=1.3 gainDb=-6", "fx", -18, -0.35)
pat("grind", grind)
fx("grind", "eq", "grind_eq", "lowCutHz=200 highCutHz=7000")
w("# --- low end: saturated bass pulse locked to the machine, motor drone that turns twice a bar")
track("Bass Pulse", "bass", "builtin:synth", "wave=square voices=1 cutoff=320 resonance=0.25 envAmount=1.5 filterDecay=0.12 attack=0.002 decay=0.25 sustain=0.5 release=0.08 sub=0.6 mono=true gainDb=-4 width=0", "bass", -7)
pat("bass", bass)
fx("bass", "saturator", "bass_sat", "driveDb=12 bias=0.2 toneHz=900 outputDb=-6 mix=0.8")
fx("bass", "eq", "bass_eq", "lowCutHz=30 highCutHz=2500")
fx("bass", "utility", "bass_mono", "bassMonoHz=160")
track("Motor Drone", "motor", "builtin:synth", f"wave=saw voices=3 detune=9 cutoff=380 resonance=0.35 attack=1.5 sustain=1 release=1.5 lfoRate={BPM/60/2:.3f} lfoPitch=0.18 lfoCutoff=1.4 lfoAmp=0.35 noise=0.18 drift=7 gainDb=-8 width=0.7", "drone", -15)
pat("motor", drone)
fx("motor", "distortion", "motor_dist", "driveDb=10 outputDb=-8 mix=0.5")
fx("motor", "bitcrush", "motor_crush", "bits=9 downsample=2 mix=0.25")
fx("motor", "eq", "motor_eq", "lowCutHz=65 highCutHz=3000 peak1Hz=250 peak1Db=-3")
w("# --- semi-liturgical: choir 'ahh' drone, tolling bell, sparse organ fifths")
track("Choir Ahh", "choir", "builtin:synth", "wave=saw voices=7 detune=16 cutoff=2200 attack=2.5 sustain=1 release=2.5 formant=a formantMix=0.85 lfoRate=0.21 lfoAmp=0.15 drift=5 gainDb=-12 width=1.0", "pad", -17)
pat("choir", choir)
fx("choir", "eq", "choir_eq", "lowCutHz=180 highCutHz=6000")
track("Toll Bell", "bell", "builtin:bell", "decay=7 brightness=0.35 beat=0.8 strike=0.4 octave=0 gainDb=-6", "bell", -13, 0.05)
pat("bell", bell)
fx("bell", "eq", "bell_eq", "lowCutHz=120")
track("Organ Fifths", "organ", "builtin:synth", "wave=triangle voices=2 detune=3 cutoff=1600 attack=0.25 sustain=1 release=1.2 sub=0.2 gainDb=-10 width=0.6", "chords", -19)
pat("organ", organ)
fx("organ", "eq", "organ_eq", "lowCutHz=110")
w("# --- returns: a huge dark cathedral, and a short iron room for the metal")
w("track add Cathedral --id cathedral --type return")
fx("cathedral", "reverb", "cath_rv", "size=0.97 damping=0.75 width=1.0 mix=1.0 lowCutHz=220 predelayMs=45")
fx("cathedral", "eq", "cath_eq", "highCutHz=5000")
w("track add \"Iron Room\" --id ironroom --type return")
fx("ironroom", "reverb", "iron_rv", "size=0.45 damping=0.3 mix=1.0 lowCutHz=300 predelayMs=8")
fx("ironroom", "bitcrush", "iron_crush", "bits=10 downsample=2 mix=0.3")
for tid, rt, lv in [("choir","cathedral",-4),("bell","cathedral",-6),("organ","cathedral",-6),("metal","cathedral",-16),
                    ("metal","ironroom",-8),("piston","ironroom",-12),("gears","ironroom",-14),("grind","cathedral",-12)]:
    w(f"send add {tid} {rt} --level {lv}")
w("# --- master: tight low end, glue, saturation, limiter")
fx("master", "eq", "master_eq", "lowCutHz=26 peak1Hz=320 peak1Db=-1.5 peak1Q=0.8")
fx("master", "compressor", "master_comp", "thresholdDb=-14 ratio=2.5 attack=0.015 release=0.18 kneeDb=8")
fx("master", "saturator", "master_sat", "driveDb=3 bias=0.05 outputDb=-2 mix=0.5")
fx("master", "limiter", "master_limiter", "ceilingDb=-1 gainDb=3")
w("# the sub kick and the bass pulse share the low end: let AIDAW make room")
w("clash report")
w("fix clash kick bass")
w("listen")
open(os.path.join(HERE, "build_industrial.pdaw"), "w").write("\n".join(L) + "\n")

# ---- Horror Engine layer (PC only): long drones that breathe with the bell, plus a rusty swell into the loop
HE = {"program": "Rust Works",          # rods, grit and steel junk: the machinery side of the plugin
      "macro_dread": 0.8, "macro_distance": 0.35, "macro_material": 0.3,
      "hurdy_gurdy_level_db": -5.0, "hurdy_gurdy_root_hz": 41.2, "hurdy_gurdy_dog_buzz": 0.45, "hurdy_gurdy_crank_rev_s": 60/BPM*2,
      "bowed_metal_object": "Rod", "bowed_metal_level_db": -6.0,
      "struck_metal_object": "Bell", "struck_metal_level_db": -12.0,
      "aura_darkness": 0.85, "aura_shimmer": 0.0, "aura_breathe": 0.6,
      "glass_mute": True, "superball_mute": True, "junk_tray_level_db": -14.0,
      "hall_size": 0.9, "hall_decay_s": 10.0, "master_tilt": -0.3, "master_volume_db": -4.0}
he_notes = [(E2, 0.0, 4*BAR - 0.2, 96), (E2, 4*BAR, 3*BAR - 0.2, 96), (F2, 7*BAR, BAR - 0.3, 100)]
H = []; h = H.append
h("# AIDAW Industrial Liturgy: Horror Engine VST3 layer (run after build_industrial.pdaw, on the PC where the plugin lives)")
h(f"track add \"Horror Engine\" --id horror --plugin \"vst3:Horror Engine\" --role drone --params '{json.dumps(HE)}'")
h("mix set horror --gain -12 --pan 0")
h(f'pattern add horror_p --lengthBeats {BARS*BAR:g} --notes "{notes(he_notes)}"')
h("clip add_pattern horror horror_p 1.1.1")
h('plugin add horror builtin:eq --id horror_eq --params "lowCutHz=90 highCutHz=9000 peak1Hz=400 peak1Db=-2"')
h('plugin add horror builtin:saturator --id horror_sat --params "driveDb=6 bias=0.1 outputDb=-4 mix=0.4"')
h("send add horror cathedral --level -10")
h("listen")
open(os.path.join(HERE, "horror_engine.pdaw"), "w").write("\n".join(H) + "\n")
print(len(L), "+", len(H), "lines;", len(kick), "kicks", len(piston), "piston", len(gears), "gears", len(bass), "bass")
