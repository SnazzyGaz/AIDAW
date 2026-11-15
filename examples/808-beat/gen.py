# Generates build_808.pdaw: an 8-bar trap loop in F minor at 144 BPM (half-time), written as AIDAW commands.
import random
random.seed(808)
NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
def nm(k): return f"{NAMES[k%12]}{k//12-1}"
def notes(lst): return " ".join(f"{nm(k) if isinstance(k,int) else k}@{round(t,4)}:{round(l,4)}:{int(v)}" for k,t,l,v in lst)
BAR = 4.0; BARS = 8
L = []
w = L.append
w("# AIDAW 808 beat: trap loop, F minor, 144 BPM (half-time feel), 8 bars, loop on")
w('project set --title "808 Beat" --author "Garyck Arntzen"')
w("tempo set --bpm 144")
w('keysig set --key "F minor"')
w("loop set 1.1.1 9.1.1")
# ---------- kick (2-bar figure, bar 8 variation)
kickA = [0, 7, 10]            # 16th steps
kickB = [0, 3, 6, 11, 14]
kick = []
for b in range(BARS):
    steps = kickA if b % 2 == 0 else kickB
    if b == 7: steps = [0, 3, 6]          # leave a gap before the loop restarts
    for st in steps: kick.append((36, b*BAR+st*0.25, 0.25, 122 if st == 0 else 112))
# ---------- 808: follows the kick, holds to the next hit, slides on chosen transitions
prog = [29, 29, 25, 25, 34, 34, 36, 36]   # F1 F1 Db1 Db1 Bb1 Bb1 C2 C2  (i VI iv V)
b808 = []
hits = []
for b in range(BARS):
    steps = kickA if b % 2 == 0 else kickB
    if b == 7: steps = [0, 3, 6]
    for st in steps: hits.append((b*BAR+st*0.25, b))
for i, (t, b) in enumerate(hits):
    end = hits[i+1][0] if i+1 < len(hits) else BARS*BAR
    k = prog[b]
    # pitch moves: octave jump on the last hit of the 2-bar figure, fifth/b7 colour notes
    st = round((t - b*BAR)/0.25)
    if b % 2 == 1 and st == 14: k = prog[b] + 12
    if b % 2 == 1 and st == 11: k = prog[b] + 7
    if b == 7 and st == 6: k = 41          # F2 pickup into the loop
    slide = (b % 2 == 1 and st in (14,)) or (b == 7 and st == 6) or (b % 2 == 1 and st == 11)
    ln = (end - t) + (0.12 if slide or (i+1 < len(hits) and b % 2 == 1) else -0.05)
    b808.append((k, t, max(0.2, ln), 118))
# make the note *before* a slide overlap the slide note so it glides into it
b808 = sorted(b808, key=lambda n: n[1])
fixed = []
for i, n in enumerate(b808):
    k, t, ln, v = n
    if i+1 < len(b808) and b808[i+1][0] != k and (b808[i+1][1] - t) < 2.0:
        ln = (b808[i+1][1] - t) + 0.1          # legato -> glide
    elif i+1 < len(b808):
        ln = (b808[i+1][1] - t) - 0.05         # same pitch: retrigger
    fixed.append((k, t, ln, v))
b808 = fixed
# ---------- clap + snare layer on beat 3 (half-time)
clap = []
for b in range(BARS):
    clap.append((39, b*BAR+2.0, 0.25, 118)); clap.append((38, b*BAR+2.0, 0.25, 92))
    if b in (3, 7): clap.append((38, b*BAR+3.75, 0.2, 70))   # ghost before the turnaround
# ---------- hats: 8ths with 16th pickups, triplet + 32nd rolls, velocity accents
hats = []
for b in range(BARS):
    o = b*BAR
    t = 0.0
    while t < BAR - 1e-9:
        if b % 2 == 1 and 3.0 <= t < 4.0 and b != 7:          # triplet-16th roll on beat 4
            for j in range(6): hats.append((42, o+3.0+j/6, 0.08, 64+j*7 + random.randint(-4,4)))
            break
        if b == 7 and 2.5 <= t:                               # 32nd roll ramping up into the loop
            for j in range(12): hats.append((42, o+2.5+j*0.125, 0.06, 50+j*5 + random.randint(-3,3)))
            break
        acc = 100 if abs(t % 1.0) < 1e-6 else 78
        hats.append((42, o+t, 0.1, acc + random.randint(-8, 8)))
        if (b in (0, 4) and abs(t-1.5) < 1e-6) or (b in (2, 6) and abs(t-0.5) < 1e-6):
            hats.append((42, o+t+0.25, 0.08, 66 + random.randint(-6,6)))       # 16th pickup
        if b in (2, 6) and abs(t-2.5) < 1e-6:
            for j in range(3): hats.append((42, o+2.5+(j+1)/6, 0.06, 58+j*6))  # quick triplet flick
        t += 0.5
openhat = [(46, b*BAR+3.5, 0.4, 88) for b in (0, 2, 4, 6)]
hats = [h for h in hats if not any(abs(h[1]-o[1]) < 1e-6 for o in openhat)]
# ---------- dark bell melody (F minor, 2-bar phrase answered)
F5=77; Ab4=68; C5=72; Db5=73; Eb5=75; Bb4=70; G4=67
phr1 = [(C5,0,0.5),(Ab4,0.75,0.5),(F5-12+12,1.5,0.75),(Eb5,2.5,0.5),(C5,3.0,1.0),(Db5,4.5,0.5),(C5,5.0,0.5),(Ab4,5.75,1.0)]
phr2 = [(C5,0,0.5),(Ab4,0.75,0.5),(F5,1.5,0.75),(Eb5,2.5,0.5),(Db5,3.0,1.0),(C5,4.5,0.5),(Bb4,5.0,0.5),(G4,5.75,1.5)]
bell = []
for rep, ph in enumerate([phr1, phr2, phr1, phr2]):
    for k, t, ln in ph: bell.append((k, rep*8+t, ln, 92 + random.randint(-6, 6)))
# ---------- pad: low, dark chords, 2 bars each
chords = [[53,56,60],[49,53,56],[46,49,53],[48,52,55]]   # Fm Db Bbm C
pad = []
for i, ch in enumerate(chords):
    for k in ch: pad.append((k, i*8, 8.0, 76))

def track(name, tid, plugin, params, role, gain, pan=None):
    w(f'track add "{name}" --id {tid} --plugin {plugin} --role {role}' + (f' --params "{params}"' if params else ""))
    w(f"mix set {tid} --gain {gain}" + (f" --pan {pan}" if pan is not None else ""))
def pat(tid, ns):
    w(f'pattern add {tid}_p --lengthBeats {BARS*BAR:g} --notes "{notes(ns)}"')
    w(f"clip add_pattern {tid} {tid}_p 1.1.1")

w("# drums")
track("Kick", "kick", "builtin:drums", "kickPitch=46 kickDecay=0.32 gainDb=-1", "kick", -2)
pat("kick", kick)
track("Clap", "clap", "builtin:drums", "gainDb=-5 snareDecay=0.18", "clap", -6)
pat("clap", clap)
track("Hats", "hats", "builtin:drums", "gainDb=-10 hatDecay=0.04 openHatDecay=0.3", "hat", -9, 0.12)
pat("hats", sorted(hats + openhat, key=lambda n: n[1]))
w("plugin add hats builtin:eq --id hats_eq --params \"lowCutHz=600 highShelfDb=2\"")
w("# 808: mono sine with slides, saturated, mono below 150 Hz")
track("808", "b808", "builtin:synth", "wave=sine voices=1 cutoff=2400 attack=0.001 decay=2.5 sustain=0.75 release=0.18 pitchEnv=5 pitchDecay=0.03 glide=0.09 gainDb=-3 width=0", "bass", -4)
pat("b808", b808)
w("plugin add b808 builtin:distortion --id b808_sat --params \"driveDb=9 outputDb=-7 mix=0.55\"")
w("plugin add b808 builtin:eq --id b808_eq --params \"lowCutHz=28\"")
w("plugin add b808 builtin:utility --id b808_mono --params \"bassMonoHz=150\"")
w("# dark bell and pad")
track("Bell", "bell", "builtin:synth", "wave=sine voices=2 detune=4 cutoff=7000 attack=0.001 decay=1.0 sustain=0.0 release=0.7 gainDb=-9 width=0.6", "lead", -14)
pat("bell", bell)
w("plugin add bell builtin:eq --id bell_eq --params \"lowCutHz=300\"")
track("Pad", "pad", "builtin:synth", "wave=saw voices=5 detune=14 cutoff=900 attack=0.8 decay=0.5 sustain=0.9 release=1.6 gainDb=-16 width=1.0", "pad", -20)
pat("pad", pad)
w("plugin add pad builtin:eq --id pad_eq --params \"lowCutHz=220\"")
w('track add Verb --id verb --type return')
w('plugin add verb builtin:reverb --id verb_rv --params "size=0.8 damping=0.5 mix=1.0 lowCutHz=300 predelayMs=25"')
w("send add bell verb --level -9")
w("send add pad verb --level -12")
w("send add clap verb --level -20")
w("# master")
w('plugin add master builtin:eq --id master_eq --params "lowCutHz=25"')
w('plugin add master builtin:compressor --id master_comp --params "thresholdDb=-12 ratio=2 attack=0.02 release=0.2 kneeDb=8"')
w('plugin add master builtin:limiter --id master_limiter --params "ceilingDb=-1 gainDb=3"')
w("# kick and 808 share the low end: let AIDAW make room")
w("clash report")
w("fix clash kick 808")
w("listen")
open("/workspace/beat/build_808.pdaw","w").write("\n".join(L)+"\n")
print(len(L), "lines;", len(kick), "kicks", len(b808), "808 notes", len(hats), "hats", len(bell), "bell")
for n in b808: print(nm(n[0]), round(n[1],3), round(n[2],3))
