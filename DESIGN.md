# PlainDAW — Internal Design Document

Status: draft v0.1 · 15 Nov 2026 · owner: Daw Man (for Garyck Arntzen, https://x.com/_mintydoge)
Inputs: research Page "DAWs, what they miss, and a plan for a basic open-source DAW for humans and AI"
(https://cursor.com/canvas/store-25d1f83b-a010-4fe2-9cc1-c483af3fd0c8/dc21b0c0-06c2-41cf-ad0f-32194f2177a3)
and the example in `/workspace/plaindaw-example/` (mockup, `song.plaindaw.json`, `session.txt`).

---

## 1. One-paragraph summary

PlainDAW is a small, free, open-source DAW where the song is a plain JSON file, every change is a named
command, and the audio engine runs with or without a screen. A person (through a thin GUI or the CLI) and
an AI agent (through JSON-RPC or MCP) drive the same command layer, share one undo history, and see the
same project. The AI "hears" through offline renders plus measured metrics (LUFS, true peak, spectrum
bands, per-track levels, masking hints). It hosts VST3 and CLAP plugins (LV2 on Linux), so the sound
palette is whatever plugins the user owns. It interoperates with FL Studio through MIDI, stems and FL's
command-line render.

## 2. Goals

1. **Complete for real songs.** Everything needed to make a finished EDM/pop/hip-hop/ambient track, or
   a foley/sound-design session, from scratch: instruments, drums, audio clips, editing, mixing,
   automation, sidechain, mastering chain, export.
2. **Text-first.** The project is human-readable, schema-validated JSON that diffs and merges well in git.
3. **One command layer.** GUI, CLI, RPC and MCP call the same ops. No feature exists only in the GUI.
4. **Headless by default.** Engine is a library + daemon; GUI optional; works on a server with no display.
5. **Deterministic offline render.** Same project + same plugin versions + same settings = same bytes
   (for deterministic plugins), with a render hash to prove it.
6. **Measurable feedback.** Every render can produce an analysis report an AI can reason about.
7. **Safe for agents.** Dry-run, read-only mode, confirmations for destructive ops, sandboxed file access,
   undo for everything.
8. **Free and open source.** GPLv3-or-later code; MIT/CC0 schema/spec so anyone can read/write the format.
9. **Small.** A codebase one or two people (plus AI help) can understand and maintain.

## 3. Non-goals

- Competing with FL Studio/Live on depth of UI polish or built-in content.
- Built-in AI music generation (the AI is a user of the DAW, not a generator inside it).
- Notation/score, video tracks (v1), surround/Atmos, AAX, VST2.
- Cloud accounts, stores, telemetry, DRM.
- A control-surface zoo (generic MIDI learn + OSC only).
- Skins/themes beyond light/dark.

## 4. Users and user stories

Actors: **Human** (Gaz: producer, FL Studio user with VST instruments, Windows PC with RTX 2080),
**AI agent** (connected via MCP/JSON-RPC), **Script** (CI, batch jobs).

### 4.1 Human
- H1: I open a song in the GUI, see timeline, piano roll, mixer, and the command log with who did what.
- H2: I play notes on my MIDI keyboard and record them into a clip; I record a vocal/foley take.
- H3: I drag a sample in, it snaps to tempo (stretch), I trim/fade/reverse it.
- H4: I undo something the AI did, even if I did other things after (selective revert of op N, if safe).
- H5: I export MIDI and stems to take the idea into FL Studio, and bring FL stems back for mixing.
- H6: I commit the song to git and see a musical diff ("Bass gain -4.5 → -6 dB; +8 notes in bass_a").
- H7: I lock tracks/params so the AI can't change them ("don't touch the lead sound").

### 4.2 AI agent
- A1: Build a song from a brief: create tracks, load plugins, write patterns, arrange sections.
- A2: Render a section, analyze it, compare against a target (e.g. -8 LUFS integrated for club, TP ≤ -1),
  propose changes, apply only with permission when the session demands it.
- A3: Read the project as JSON, reason about arrangement (bars, sections, density), and explain it.
- A4: Batch jobs: render 20 variations, loudness-match them, report differences.
- A5: Analyze FL Studio projects by rendering them with `FL.exe /R` and running `plaindaw analyze-file`.

### 4.3 Worked story: big-room EDM track from scratch (style of e.g. 2013 big-room, no copying)
Target: 128 BPM, F minor, ~3:30, structure Intro 16 · Build 16 · Drop 32 · Break 16 · Build 16 · Drop 32 · Outro 16.
1. `project.new --bpm 128 --key Fm --sig 4/4`; markers for each section (`marker.add`).
2. Drums: `track.add audio|instrument "Kick"` with built-in sampler (`sampler.load kick.wav`), pattern
   four-on-the-floor; clap on 2/4; open hats offbeat; `pattern.fill` helpers or explicit notes.
3. Lead: `track.add instrument "Lead"` + `plugin.add clap:...`/`vst3:Serum 2` (user-owned), preset load,
   write the drop riff as a pattern (`pattern.add_notes`), layer a second synth on another track,
   group both into a "Lead bus" (`track.add bus`, `track.route`).
4. Sidechain: `sidechain.set lead_bus --source kick --plugin builtin:compressor` or volume-shaper on the bus.
5. Build-ups: snare roll pattern with velocity ramp (`notes.ramp_velocity`), riser (synth with filter
   automation `auto.add ... cutoff`), white-noise sweep, pitch automation, tempo stays 128.
6. Drop impact: downlifter/impact sample on bar 1 of drop, reverb send automation to kill tail.
7. Break: pad chords, piano, filter on master bus automated.
8. Mix: gain staging (`analyze --per-track`), EQ low-cut non-bass tracks, sends to reverb/delay returns.
9. Master: `builtin:eq → builtin:compressor → builtin:limiter` or user plugins; target loudness.
10. AI loop: `render --range Drop1` → `analyze` → "kick/bass overlap 50–90 Hz, lead masks vocal chop at
    2–4 kHz, drop is only +3.1 LU over build" → proposes changes → human approves → re-render → compare.
11. Export: `export.mix --wav --mp3`, `export.stems`, `export.midi`.
Everything above is plain notes, samples, plugins, automation, routing and sidechain: all v1 features.
The character of the sound comes from the plugins and samples, not the DAW.

### 4.4 Worked story: foley / sound editing
1. Import or record a take (footsteps, door, cloth). `clip.split` at transients (`clip.detect_transients`),
   `clip.trim`, `clip.fade`, `clip.reverse`, `clip.gain`, `clip.normalize`.
2. Per-clip pitch/stretch (`clip.set --pitch -3 --stretch 1.2`), region FX (clip-level plugin chain, later)
   or a track insert chain (EQ, compressor, reverb).
3. Layer hits on stacked tracks, nudge in ms (`clip.move --by 12ms`), crossfades.
4. Batch: "normalize all clips on Foley to -1 dBFS peak, trim silence below -50 dB, export each as WAV"
   (`clip.export --each`, `batch` ops).
5. Analysis: per-clip peak, RMS, duration, spectral centroid, onset list.
Video sync is "later"; v1 supports a timecode/marker list imported from a file (e.g. EDL/CSV of hit times).

## 5. Feature scope

### 5.1 v1 (must have)
**Transport/time:** tempo map (changes, linear ramps later), time signature changes, key signature
(metadata, used by helpers), loop range, metronome with count-in, markers and named sections,
bars.beats.ticks + seconds + samples conversions, PPQ 960.

**Tracks:** audio, instrument, bus (group), return (FX), master; folders (as buses with `collapsed` UI hint);
colours; mute/solo/arm/monitor; track lock (human/AI write protection); freeze (render-in-place) for CPU.

**Clips & patterns:**
- Audio clips: file ref, start, length, source offset, gain, fades (in/out curves), crossfades,
  reverse, pitch (semitones/cents), stretch modes (off / tempo-sync / fixed ratio), loop within clip, split/trim.
- MIDI patterns: notes (start, length, key, velocity, release velocity), CC lanes, pitch bend, channel
  pressure; patterns reusable by reference (linked clips); per-clip transpose and velocity offset;
  "make unique" to unlink.
- Automation clips/lanes on any param (mix, sends, plugin params, tempo later): points with curve types
  (linear, hold, smooth/bezier-lite).

**Mixer:** gain, pan (balance; stereo width utility plugin), mute/solo, polarity, insert chain (any length,
bypass, wet/dry for built-ins), pre/post-fader sends, sidechain inputs to plugins, PDC, master chain,
peak/RMS meters, LUFS meter on master.

**Plugins:** VST3, CLAP (all platforms), LV2 (Linux); scan + cache; preset I/O (`.vstpreset`, CLAP state);
param list/introspection with names, units, ranges, display strings; parameter automation; plugin GUIs
open in the GUI client only; MIDI-learn.

**Built-in devices (small, good):** sampler (one-shot + multi-zone + slice), simple subtractive synth
(2 osc + noise, filter, 2 env, LFO), drum-rack style "kit" (pads → sampler), EQ (8-band param + low/high cut),
compressor (with sidechain), limiter (true-peak), gate, reverb, delay (sync), chorus, distortion/saturator,
filter, utility (gain/pan/width/mono/polarity), volume shaper (sidechain-style ducking), spectrum/meter.
(Most come from Tracktion Engine built-ins; fill gaps ourselves.)

**Recording:** audio input (mono/stereo), MIDI input, punch in/out, loop recording into takes (simple:
keep all takes, pick active one; full comping later), input monitoring, latency compensation.

**I/O & interop:** import WAV/AIFF/FLAC/OGG/MP3 (+ others via ffmpeg), SMF type 0/1 import/export, export
mix (WAV 16/24/32f, FLAC, MP3 via ffmpeg/LAME), stems (per track, per bus, or selection), render range,
render-in-place, DAWproject import/export in v1.x (M5).

**Control:** CLI, JSON-RPC 2.0 (WebSocket + stdio), MCP server, operation log, undo/redo, transactions,
dry-run, read-only sessions, permissions (ask/allow per op class), project lock file.

**Analysis:** see §9.

**GUI (thin, web):** timeline, piano roll, mixer, clip/waveform editor, browser for files/plugins,
command log with author tags, analysis panel; all edits go through ops.

### 5.2 Later
Out-of-process plugin sandbox (M5 target, may slip post-1.0) · full comping · clip launcher/scenes ·
MPE/MIDI 2.0 per-note expression · modulators (LFO/envelope/sidechain-follower on any param) ·
Ableton Link · video track + timecode sync · AU on macOS · real-time analysis streaming while playing ·
region/clip FX chains · tempo ramps · stem separation via external model (Demucs, runs on GPU) ·
collaborative live editing (op-log sync) · audio-to-MIDI · scripting plugins (JS/Lua user ops).

### 5.3 Deliberately left out
Built-in generative AI; sample store/cloud; notation; Atmos/surround; VST2/AAX; ReWire; large built-in
instrument library; control-surface integrations beyond MIDI learn/OSC; skins.

## 6. Architecture

```
 Human ──GUI (web, TS)──┐
 Human ──CLI────────────┤                     ┌──> Plugin host (in-proc v1; sandbox proc later)
 AI ─────MCP server─────┼──> JSON-RPC API ──> Command/Op layer ──> Project model ──> Engine adapter ──> Tracktion Engine
 Scripts ─JSON-RPC──────┘         ▲            (validate, apply,      (in-memory,        (graph build,       ├─> Audio/MIDI devices
                                  │             undo, log, perms)      JSON <-> model)    incremental sync)   └─> Offline renderer ─> Analysis
                                  └────────────────── events (state changes, meters, analysis results) ─────────────────┘
```

Process model: one daemon `plaindawd` per open project (or per user with multiple projects). The CLI is a
thin client: if a daemon is running for the project it talks to it; otherwise it loads the project
in-process, applies the op, saves, exits ("one-shot mode"). GUI and MCP are clients of the daemon.

### 6.1 Engine
- C++20 on **Tracktion Engine** (GPLv3+) + **JUCE 8** (AGPLv3).
- `EngineAdapter` maps our model → `te::Edit` (tracks, clips, plugins, automation, routing). Our JSON
  is the source of truth; the Edit is a derived runtime object, never saved as our format (may be cached).
- Incremental sync: each op emits a typed "change set" (paths changed); the adapter updates only the
  affected Edit objects. Full rebuild is the fallback and a test oracle (incremental == rebuild).
- Real-time thread rules: no allocation/locks in audio callback; model changes posted via lock-free queue.
- Offline render: Tracktion `Renderer` with fixed block size, sample rate, and seeded built-ins.
- Time-stretch: Tracktion's supported stretchers; default Rubber Band (GPL) or SoundTouch; record the
  algorithm + version in render metadata for determinism.

### 6.2 Command / op layer
- Every mutation is an **op**: `{ "op": "mix.set", "args": {...}, "author": "ai|human:<id>|script",
  "ts": "...", "id": 412, "txn": "t-88" }`.
- Pipeline: parse → schema-validate args → permission check (locks, read-only, ask-before classes) →
  precondition check (targets exist, ranges) → compute **inverse op** → apply to model → emit change set →
  append to `history/ops.jsonl` → autosave (debounced) → broadcast event.
- **Transactions:** `txn.begin/commit/rollback`; a txn undoes as one step (e.g. "build drop" = 40 ops).
- **Dry-run:** returns the change set + diff without applying.
- **Undo/redo:** linear stack of inverse ops, shared by all authors. `history.revert <id>` = selective
  undo: applies inverse of op N if later ops don't touch the same paths; otherwise refuses with conflict list.
- **Ids:** stable string ids for tracks/clips/patterns/plugins/automation (`trk_bass`, short random suffix
  if auto-generated). Ops target ids, never array indexes.
- Op handlers are pure functions on the model where possible → easy unit tests.

### 6.3 Project format
Folder (`*.plaindaw/` or any folder with `project.json`):
```
project.json        # the song (source of truth)
media/              # content-addressed audio: <sha256-12>.<ext>, immutable
states/             # plugin state blobs: <plugin-instance-id>.<vstpreset|clapstate|bin>
history/ops.jsonl   # op log (append-only), rotated; snapshots/ every N ops
renders/            # outputs + analysis JSON (gitignored)
cache/              # peaks/waveform overviews, plugin scan, Edit cache (gitignored)
.plaindaw-lock      # daemon lock (pid, host)
```
Rules:
- JSON Schema (draft 2020-12) published as `schema/plaindaw-0.x.json`, MIT/CC0.
- Canonical writer: stable key order, 2-space indent, one note per line, numbers formatted minimally,
  trailing newline → clean git diffs. `plaindaw fmt` rewrites canonically.
- Times: musical positions as `"bar.beat.tick"` strings or beat floats in patterns; absolute seconds only
  where musically meaningless (fades, `offsetSec`).
- `format` field carries version; migrations `0.1→0.2…` are code + tests; loader refuses unknown majors.
- Plugin references: `"clap:<id>"`, `"vst3:<name>|<uid>"`, `"lv2:<uri>"`, `"builtin:<name>"`; plus
  `pluginVersion` recorded for reproducibility; missing plugin → placeholder (passes audio through or
  silent instrument) + warning, project still loads.
- Plugin params stored as normalized + display value for human readability; state blob is authoritative
  for third-party plugins, params mirror is informational unless automated.
- Media: import copies into `media/` by hash (option `--link` to reference external). Unused media GC
  via `media.gc`.
- Git: `.gitattributes` suggests LFS for `media/` and `states/`.

Schema sketch: see `/workspace/plaindaw-example/song.plaindaw.json` (meta, settings, transport, patterns,
tracks[type, clips, instrument, inserts, mix, sends, output, automation], markers). Additions for v1:
`keySig`, `track.locked`, `track.color`, `clip.pitch`, `clip.reverse`, `clip.takes`, `sidechain` on
inserts (`"sidechain": "trk_kick"`), `pattern.cc`, `pattern.pitchBend`, `automation.curve`, `groups`.

### 6.4 Plugin hosting and sandboxing
- v1: in-process hosting via JUCE (VST3, LV2) + our **CLAP host adapter** (CLAP headers MIT; wrap as a
  `te::Plugin` subclass; implement params, state, note ports, audio ports, latency, GUI ext, thread-check).
- Scanning out of process (child `plaindaw-scan`), with a blocklist of crashers; cache in user config.
- **Sandbox (later, M5):** `plaindaw-plughost` child processes; audio/MIDI via shared-memory ring buffers,
  control via pipe; modes: in-process / per-plugin / per-vendor group (like Bitwig). Crash → plugin
  replaced with bypass placeholder, session continues, event `plugin.crashed`.
- Offline render: call plugins with `offline` flag/`isNonRealtime`; warn on plugins that ignore it.
- Determinism probe: `plugin.check-determinism` renders a test twice and compares hashes; result cached and
  shown in analysis metadata.

### 6.5 Render and analysis
- `render` targets: full song, range (bars or marker names), tracks/stems, buses, pre/post master chain.
- Settings: sample rate, bit depth, dither (TPDF on/off), normalize (off by default), tail length, realtime
  fallback for plugins that need it.
- Output metadata sidecar `*.render.json`: project hash, op id, engine version, plugin versions, settings,
  output sha256, wall time.
- Analysis runs on render output (or any file via `analyze-file`); see §9.

### 6.6 CLI
`plaindaw <noun> <verb> <project> [args]` mirrors op names (`mix set` ↔ `mix.set`). Global flags:
`--json` (machine output), `--dry-run`, `--as ai|human`, `--yes` (skip confirmations), `--txn`.
Exit codes: 0 ok, 2 validation, 3 permission, 4 conflict, 5 engine/plugin error.

### 6.7 JSON-RPC
JSON-RPC 2.0 over WebSocket (`ws://127.0.0.1:7700`, token auth, localhost only by default) and stdio.
Methods = op names + queries (`project.get`, `project.query` with JSONPath, `track.list`, `plugin.params`,
`history.list`, `transport.state`), plus subscriptions (`events.subscribe` → `op.applied`,
`transport.position`, `meters`, `render.progress`, `analysis.ready`, `plugin.crashed`).

### 6.8 MCP server
`plaindaw-mcp` (Python or TS, using the official MCP SDK) wraps JSON-RPC. Tools grouped and kept
coarse-grained to limit tool count (~25 tools): `project_overview`, `get_section`, `edit` (generic: op +
args, schema in description), `add_track`, `write_pattern`, `arrange`, `set_mix`, `add_plugin`,
`set_params`, `automate`, `render`, `analyze`, `compare_renders`, `undo`, `history`, `list_plugins`,
`find_samples`, `export`. Resources: `project.json`, latest analysis, op log tail. Permission model:
session setting `ask | allow-nondestructive | allow-all`; destructive ops (delete track, media.gc, overwrite
render, project.close without save) always confirm unless `allow-all`.

### 6.9 Thin GUI
- Web app (TypeScript, Canvas/WebGL for timeline and roll, no heavy framework requirement; Svelte or
  React acceptable). Served by the daemon on localhost; can be wrapped in a desktop shell later.
- Views: arrangement, piano roll, mixer, clip editor (waveform), browser, command log (author colours,
  undone ops greyed), analysis panel (LUFS/TP, band chart, section loudness, warnings), plugin list.
- Plugin native GUIs: opened by the daemon as native windows when a display exists (v1); not in browser.
- Latency: optimistic UI for drags, reconciled by `op.applied` events.
- Design: dark/light, clean, see `/workspace/plaindaw-example/plaindaw-mockup.png`.

## 7. Command / op list (v1)

Conventions: all ops accept `dryRun`; targets by id; positions as `"b.b.t"` or `{beats}`; returns
`{op, changed[], before?, after?}`.

**project**: `project.new`, `project.open`, `project.save`, `project.save_as`, `project.close`,
`project.set` (title, author, sampleRate, ppq), `project.validate`, `project.fmt`, `project.stats`.
**transport** (non-undoable, not logged as edits): `transport.play`, `.stop`, `.record`, `.locate`,
`.set_loop`, `.metronome`, `.count_in`.
**time** (undoable): `tempo.set`, `tempo.add_change`, `tempo.remove_change`, `timesig.add_change`,
`timesig.remove_change`, `keysig.set`, `marker.add`, `marker.move`, `marker.rename`, `marker.remove`,
`section.insert_bars`, `section.delete_bars`, `section.duplicate` (ripple: shifts clips/automation/markers).
**track**: `track.add`, `track.remove`, `track.rename`, `track.move` (order), `track.set` (color, mute,
solo, arm, monitor, locked), `track.route` (output), `track.duplicate`, `track.freeze`, `track.unfreeze`.
**clip (audio)**: `clip.add`, `clip.remove`, `clip.move`, `clip.copy`, `clip.split`, `clip.trim`,
`clip.set` (gain, pitch, reverse, stretch, loop), `clip.fade`, `clip.crossfade`, `clip.normalize`,
`clip.detect_transients` (query), `clip.slice_to_sampler`, `clip.bounce` (consolidate), `clip.take_select`.
**pattern / MIDI**: `pattern.add`, `pattern.remove`, `pattern.rename`, `pattern.duplicate`,
`pattern.set_length`, `pattern.add_notes`, `pattern.remove_notes` (by id or filter), `pattern.edit_notes`
(set fields on selection), `pattern.quantize`, `pattern.transpose`, `pattern.humanize` (seeded),
`pattern.ramp_velocity`, `pattern.legato`, `pattern.set_cc`, `pattern.set_pitchbend`,
`clip.add_pattern` (place pattern), `clip.make_unique`. (`clip.add_notes` in the example = alias of
`pattern.add_notes` on the clip's pattern.)
**mixer**: `mix.set` (gainDb, pan, mute, solo, polarity), `send.add`, `send.set`, `send.remove`,
`sidechain.set`, `sidechain.clear`.
**plugin**: `plugin.scan`, `plugin.list` (query), `plugin.add` (track, slot or instrument), `plugin.remove`,
`plugin.move`, `plugin.bypass`, `plugin.set_params`, `plugin.params` (query), `plugin.preset_load`,
`plugin.preset_save`, `plugin.state_snapshot`, `plugin.replace` (swap keeping slot), `plugin.open_gui`
(GUI clients only), `plugin.check_determinism`.
**sampler/kit** (built-ins): `sampler.load`, `sampler.set_zone`, `kit.add_pad`, `kit.set_pad`.
**automation**: `auto.add_lane`, `auto.remove_lane`, `auto.set_points`, `auto.add_points`,
`auto.remove_points`, `auto.clear_range`, `auto.set_curve`, `auto.from_shape` (ramp, sine, saw, step,
seeded random; for risers/filter sweeps).
**recording**: `record.arm`, `record.set_input`, `record.punch`, `record.takes` (query).
**media**: `media.import` (copy/hash; ffmpeg decode for odd formats), `media.list`, `media.info`,
`media.gc`, `media.relink`.
**render/export**: `render` (mix/range/stems/tracks), `export.midi`, `export.stems`, `export.mix`,
`export.dawproject`, `import.midi`, `import.stems` (FL round-trip), `import.dawproject`, `import.flp_summary`
(read-only via PyFLP).
**analysis** (queries, logged but not undoable): `analyze` (latest render or render+analyze),
`analyze.file`, `analyze.compare` (A/B two renders, loudness-matched), `analyze.track`, `analyze.section`.
**history**: `undo`, `redo`, `history.list`, `history.revert <id>`, `txn.begin`, `txn.commit`,
`txn.rollback`, `snapshot.save`, `snapshot.restore`, `diff` (musical diff vs snapshot/git rev).
**session/permissions**: `session.set_mode` (read-only, ask, allow), `lock.set` / `lock.clear` (paths),
`session.whoami`.
**batch**: `batch` (array of ops, all-or-nothing), `foreach` (apply an op to each id matching a query).

## 8. Undo / history details
- One shared linear stack per project; entries tagged with author.
- Non-undoable: transport, queries, analysis, renders (renders are logged).
- Op log persisted; on reopen, undo history is restored from log (inverse ops stored with each entry).
- Snapshots: full `project.json` copy every 200 ops or on demand → fast restore and log compaction.
- Conflicts on `history.revert`: if later ops touched the same JSON paths, refuse and list op ids.
- Crash safety: write op to log (fsync) before applying to model; on startup replay log beyond last save.

## 9. Analysis metrics
Per render (master) and optionally per track/bus/section:
- Loudness (EBU R128 / ITU-R BS.1770 via libebur128): integrated LUFS, short-term max, momentary max,
  loudness range (LRA), true peak dBTP (4× oversampled), sample peak.
- Dynamics: RMS, crest factor (peak − RMS), PLR (peak-to-loudness), gain-reduction stats from built-in
  limiter/compressor (if present).
- Spectrum: 1/3-octave or 7 named bands (sub 20–60, low 60–250, low-mid 250–500, mid 500–2k, high-mid 2–4k,
  presence 4–8k, air 8–20k) average dB; spectral centroid, rolloff; tilt vs pink reference.
- Stereo: correlation (−1..1), width (side/mid ratio), mono-compat loss in dB, low-end mono check (<120 Hz).
- Health: clipped samples, DC offset, silence at start/end, inter-sample overs, denormal/NaN detection.
- Musical/time: section loudness by marker, drop-vs-build contrast (LU difference), onset density per bar,
  tempo/beat-grid check of audio clips, estimated key of a render (optional, via simple chroma).
- Interaction: masking hints = per-band overlap between track pairs (energy in same band within X dB) at
  sections; kick/bass overlap; sidechain effectiveness (ducking depth measured on bus).
- Comparison: `analyze.compare` A/B with loudness matching, per-band deltas, null-test residual level.
- Targets: optional profiles (`club -8 LUFS / TP -1`, `streaming -14 LUFS / TP -1`, `podcast -16`)
  producing pass/warn lines.
Output: JSON (schema'd), plus optional PNG plots (spectrum, loudness over time) for GUI/human only.
Analysis numbers in examples are illustrative until measured.

## 10. FL Studio round-trip
- FL → PlainDAW: in FL export MIDI (File > Export > MIDI) and WAV stems with "Split mixer tracks"; then
  `plaindaw import.stems ./stems --bpm 128 --align 1.1.1` + `import.midi song.mid` (tracks by channel).
- PlainDAW → FL: `export.stems` (named `NN_trackname.wav`, same length, start at bar 1) + `export.midi`
  per track; drag into FL Playlist/Channel Rack.
- FL batch render: `FL.exe /R /Ewav /O"D:\BotWorkshop\renders" song.flp` → `plaindaw analyze.file` (runs on
  Gaz's PC only with his approval, in D:\BotWorkshop, nothing installed to C:).
- FLP reading: `import.flp_summary` via PyFLP (GPLv3, last release 2023) → read-only summary (tempo,
  patterns, channels, mixer inserts, plugin names). Not a conversion path; may fail on new FL versions.
- No DAWproject in FL; MIDI+stems remains the bridge. Same VST3/CLAP plugins on both sides; presets via
  `.vstpreset`/vendor formats, not `.fst`.
- Optional: an FL Studio Piano Roll script (`.pyscript`) that exports the open pattern as PlainDAW
  pattern JSON to clipboard/file — tiny, useful, user-installable.

## 11. Tech stack and licences
| Area | Choice | Licence |
|---|---|---|
| Engine | Tracktion Engine | GPLv3+ / commercial |
| Framework, VST3/LV2/AU hosting, devices | JUCE 8 | AGPLv3 / commercial |
| CLAP | CLAP SDK + own host adapter (clap-helpers) | MIT |
| VST3 SDK | Steinberg 3.8 | MIT |
| LV2 | lilv/serd/sord | ISC |
| ASIO (Windows) | Steinberg ASIO SDK | GPLv3 option |
| File I/O | libsndfile; ffmpeg as subprocess | LGPL-2.1+; LGPL/GPL |
| Stretch | Rubber Band (GPL) / SoundTouch (LGPL) via Tracktion | GPLv2+ / LGPL |
| Loudness | libebur128 | MIT |
| FFT | JUCE dsp / pffft | AGPL(JUCE) / BSD |
| JSON + schema | nlohmann/json + json-schema-validator | MIT |
| RPC | uWebSockets or JUCE sockets; own JSON-RPC | Apache-2 / AGPL |
| CLI parsing | CLI11 | BSD-3 |
| MCP server | official MCP Python SDK | MIT |
| Prototype/tests | Python: DawDreamer, pedalboard, mido, pyloudnorm, numpy | GPLv3, GPLv3, MIT, MIT, BSD |
| GUI | TypeScript + Svelte/React, Canvas/WebGL | MIT deps |
| Build | CMake + CPM, GitHub Actions (Win/Linux/macOS) | — |
Project licence: **GPLv3-or-later** (code); JUCE parts make conveyed binaries AGPLv3-bound. Schema/spec
and example projects: **MIT or CC0**. Docs: CC-BY-4.0. Contributors: DCO sign-off, no CLA.
Option kept open: fully permissive stack (MIT) if ever needed — would replace Tracktion/JUCE.

## 12. Repo layout
```
plaindaw/
  README.md  LICENSE  DESIGN.md  CONTRIBUTING.md
  schema/                 plaindaw-0.1.json, migrations/, examples/ (MIT/CC0)
  core/                   C++: model/, ops/, history/, schema/, io/ (media, midi), canonical JSON writer
  engine/                 C++: adapter to Tracktion (edit_builder, sync, render), clap_host/, builtins/
  analysis/               C++: loudness, spectrum, stereo, masking, compare (+ Python reference impl)
  daemon/                 plaindawd: JSON-RPC server, events, sessions, permissions
  cli/                    plaindaw CLI (thin client + one-shot mode)
  plughost/               sandbox child process (later)
  mcp/                    Python MCP server wrapping JSON-RPC
  gui/                    TypeScript web client
  proto/                  Python prototype renderer (M0), kept as reference + test oracle
  tools/                  fl-roundtrip/, pyscript exporter, flp_summary
  tests/
    unit/  ops/  schema/  golden/ (projects + expected hashes/metrics)  fuzz/  e2e/ (CLI scripts)
  third_party/            submodules (tracktion_engine, JUCE, clap, ...)
```

## 13. Testing
- **Unit:** every op: apply → inverse → model equals original (property test with random op sequences).
- **Schema:** all example projects validate; canonical writer is idempotent (`fmt(fmt(x)) == fmt(x)`);
  load→save round-trip is byte-identical.
- **Incremental vs rebuild:** after random op sequences, incrementally synced Edit renders identical to
  a freshly built Edit.
- **Golden renders:** `tests/golden/*` projects using built-ins only → render at 48k/24-bit, compare sha256;
  on mismatch, fall back to tolerance check (null-test residual < −120 dBFS) and report. Hashes pinned per
  platform+engine version; update requires explicit `--bless`.
- **Determinism:** render same project 3× and on 2 thread counts → identical; seeded humanize/random.
- **Analysis accuracy:** EBU R128 conformance test signals (EBU Tech 3341/3342) must pass within ±0.1 LU;
  sine/pink-noise fixtures for bands, peaks, correlation.
- **Plugin tests:** a small set of free CLAP/VST3/LV2 plugins (e.g. Surge XT, Vital-free alternatives, LSP)
  in CI where licences allow; crash plugin fixture for sandbox tests.
- **Fuzz:** project JSON loader and op args (libFuzzer); malformed media files.
- **E2E:** CLI scripts building the example song from scratch and the big-room template; MCP tool calls
  replayed from recorded sessions.
- **Performance:** render 3-minute 30-track built-ins project faster than 10× real time on CI runner;
  op apply p95 < 5 ms; GUI timeline 60 fps with 2k clips.
- **Interop:** MIDI export→import equality; stems sum ≈ mix (null test with master chain bypassed);
  DAWproject round-trip against Bitwig/Studio Pro sample files.

## 14. Milestones and acceptance criteria
**M0 — Spec + prototype (2–3 weeks)**
- Schema 0.1 + op list frozen as draft; Python prototype (`proto/`) loads `song.plaindaw.json`, renders
  built-in-equivalent synth/sampler + one VST3 via DawDreamer, outputs analysis JSON.
- Accept: example song renders; analysis passes EBU test signals; 30 ops implemented in Python with undo.

**M1 — Headless core (6–8 weeks)**
- C++ model, canonical JSON, op layer (≥60 ops), Tracktion adapter, built-ins, VST3 hosting, offline
  render, analysis in C++.
- Accept: big-room template project (built-ins only) renders deterministically on Linux+Windows; golden
  tests green; CLI one-shot mode builds the example song from a script.

**M2 — Control surfaces (4–6 weeks)**
- Daemon + JSON-RPC + events, full CLI, MCP server, transactions, dry-run, permissions, history.revert,
  MIDI import/export, stems, musical diff.
- Accept: an AI agent via MCP builds a 16-bar drop from a text brief, renders, analyzes, applies one
  approved fix, and the human undoes one AI op — all reflected in the op log.

**M3 — Real-time + CLAP + recording (6–8 weeks)**
- Device I/O (ASIO/WASAPI, CoreAudio, JACK/PipeWire), playback, MIDI in, audio/MIDI recording, takes,
  metronome, CLAP adapter, LV2, plugin GUIs (native windows), freeze.
- Accept: record a MIDI part and a mic take on Windows with ASIO at 128-sample buffer without dropouts;
  Surge XT (CLAP) and a VST3 synth play and automate; latency compensation verified by loopback test.

**M4 — Thin GUI (6–8 weeks)**
- Web client: arrangement, piano roll, mixer, clip editor, command log, analysis panel.
- Accept: the whole big-room story (§4.3) can be done by a human using only the GUI; every GUI action
  appears as an op; GUI and AI edit concurrently without desync.

**M5 — Robustness + interop (4–6 weeks)**
- Plugin sandbox (per-plugin mode), DAWproject import/export, FL round-trip tools, flp_summary, crash
  recovery, performance targets.
- Accept: killing a sandboxed plugin process mid-playback leaves the session running; DAWproject file from
  Bitwig imports with notes/automation intact; FL stems+MIDI round-trip lines up sample-accurately.

**v1.0** — schema 1.0 frozen with migrations; Windows + Linux installers (macOS best effort); docs, example
songs, MCP guide. Accept: two complete tracks (one EDM, one foley/sound-design scene) made end-to-end.

Estimates assume one developer with heavy AI assistance; total ≈ 7–9 months to 1.0.

## 15. Risks and open questions
**Risks**
- *Plugin non-determinism* (random seeds, time-based, real-time-only behaviour) breaks golden renders →
  determinism probe, tolerance checks, per-plugin flags.
- *CLAP host adapter effort* inside Tracktion's plugin model is non-trivial (threading, params, GUI) →
  start in M3 with Surge XT as reference; consider upstreaming.
- *JUCE AGPL obligations* if anyone hosts PlainDAW as a web service → document clearly; keep daemon
  localhost by default.
- *Licensing creep* (Rubber Band GPLv2+ vs GPLv3 OK; avoid GPLv2-only deps; Elastique is commercial — skip).
- *Real-time performance* with a JSON source-of-truth model → model edits off the audio thread, incremental
  sync; profile early.
- *Scope creep* toward a full commercial DAW → this document's non-goals are the guard; new features need
  an op, a test and a user story.
- *Windows specifics* (ASIO, plugin GUIs, DPI) on Gaz's machine → test on Windows from M1.
- *FL changes* (FLP format, CLI flags) → treat FL bridge as best-effort tooling, not core.
- *Agent safety* (AI deleting work) → permissions, locks, undo, snapshots, op log; never auto-overwrite
  media.
- *Maintenance load* of a GUI → keep it thin; GUI is a client, can lag behind the API.

**Open questions**
1. GUI tech: pure web served by daemon vs desktop shell (Tauri) for native plugin windows embedding?
2. Daemon per project or per user? (Leaning per user, multiple projects.)
3. Pattern model: FL-style global patterns (current) vs per-clip MIDI by default with optional linking?
4. Time representation in JSON: `"b.b.t"` strings vs beat floats everywhere (strings are readable but
   tempo-map dependent for meter changes) — keep both, define precedence?
5. How much of the built-in device set comes from Tracktion vs our own DSP?
6. Should MCP expose a generic `edit(op,args)` tool only, or many specific tools? (Plan: both, small set.)
7. Real-time analysis streaming in v1 or later? (Plan: later; render-then-analyze in v1.)
8. Stem separation (Demucs, MIT) as an optional GPU tool on Gaz's RTX 2080 — v1.x add-on?
9. Project name: "PlainDAW" is a working name; check for clashes before publishing.
10. Licence: confirm GPLv3-or-later vs AGPLv3 for the whole repo (AGPL would remove ambiguity with JUCE).

---

## Implementation status (v0.1, 2026-11-15)

Built in Python (GPL-3.0-or-later) + two small C libraries (CLAP host `pdclap`, DSP kernels `pddsp`, compiled on first use; the DSP one has a pure-Python fallback).

Done and tested (54 pytest tests, all passing on Linux):
- Model: canonical JSON, normalize/validate, JSON Schema, content-addressed media, tempo map + time-signature changes, bar.beat.16th.tick positions.
- Op layer: 58 ops with typed args, positional CLI forms, destructive flags, batch (atomic, one undo step), dry-run.
- History: forward/inverse patches (difflib-aligned list diffs, id-aware), undo/redo, revert-one-edit with conflict check, author tags, external-edit detection, locks (against ai/all), modes allow/read-only/ask.
- Render: graph order, buses/returns/sends pre/post, mute/solo, fader/pan automation, insert param automation, sidechain, pattern clips (repeat/transpose/velocity), audio clips (offset, fades, reverse, loop, tempo/ratio stretch, varispeed, pitch), built-in synth/drums/sampler and 11 effects, deterministic seeding.
- Plugins: VST3 via pedalboard, CLAP via own host; subprocess sandbox per plugin job; crash -> bypass + warning. Verified with Surge XT (VST3 + CLAP, instrument + effects) and own test plugins.
- Analysis: BS.1770-4 loudness (matches pyloudnorm within 0.1 LU, EBU -23 reference), true peak (4x), LRA, crest, PLR, correlation/width/mono loss, low-end mono, bands, centroid, DC, clipping, silence, sections from markers, per-track, masking hints, targets, A/B compare.
- I/O: WAV/FLAC/AIFF native, MP3/OGG/Opus/M4A via ffmpeg, stems, MIDI import/export, stem import, musical diff.
- Front ends: CLI (+ scripts), JSON-RPC 2.0 stdio, HTTP (+ SSE live feed), MCP stdio server (self-contained protocol implementation, tested with the official MCP Python SDK client), browser GUI (timeline with drag-move, piano roll add/remove, mixer faders/pan/mute/solo, history with revert, render + playback + analysis, live updates).
- Demo: examples/demo/build_demo.pdaw (original track built from commands, including an analysis-driven mix pass).

Not done yet: real-time playback/recording engine, plugin GUIs, audio-clip editing in the GUI, automation drawing in the GUI, Windows/macOS testing, FL Studio `/R /Ewav` automation helper, Rubber Band-quality stretch option, LV2.

## Implementation status (v0.2, 2026-11-15): AI features and kit

User-facing name is now **AIDAW** (repo SnazzyGaz/AIDAW). Package and CLI stay `plaindaw`; `aidaw` is an alias entry point.

Done and tested on Linux (82 pytest tests, all passing; the 54 v0.1 tests unchanged):
- **Listening** (`plaindaw/listen.py`, NumPy/SciPy only): key (Krumhansl-Kessler correlation re-scored with the detected chords), chords per bar, tempo with beat grid, timing tightness (joint tempo/phase grid fit on refined onsets: median/p90 ms off grid, feel), transients, per-track timbre tags, section energy and per-bar energy, plain-English report + JSON, a note when the heard key differs from the project key, reference comparison (`--reference`), and a listener plugin hook (`PLAINDAW_LISTENERS=module:function`, results under `extra`). No embedding model is bundled.
- **Musical commands** (`plaindaw/musical.py`, theory tables in `plaindaw/theory.py`): compose (bass/chords/pad/lead/arp/drums/song, by genre, key, progression, bars, style, seed, engine), arrange (form, markers, rolls, risers with exp filter sweeps, crashes, per-section level automation on a utility insert), fix clash (sidechain and/or EQ, applied only if the clash index drops), clash report, variations (seeds or `--vary name=lo..hi`, 10 metrics, keep best or list), humanize (feels) and pattern.swing, preview (bars/sections/tracks). Each is one batch = one undo step, with the reason stored in history and shown by `history`, the GUI and MCP. Recomposing an arranged part swaps the pattern in every section.
- **Clash metric**: per band, the share of the protected track's power that the other track covers (PB/(PA+PB)), weighted by the protected track's energy; protected = drums over melodic parts, then by role priority (kick, bass, lead, …).
- **Sound library** (`plaindaw/library.py`): scan (incremental), Surge `.fxp` metadata, `.vstpreset` class IDs, `.sfz`, audio auto-tagging (type, timbre, bpm for loops, key/root for tonal sounds), search with synonyms/key/bpm filters, audition renders, `lib use`.
- **SFZ** (`plaindaw/sfz.py`): parser + built-in sample player; uses `sfizz_render` when the kit has it.
- **Kit** (`plaindaw/kit.py`, pins in `kit_pins.json`): Surge XT 1.3.4 plugins-only release builds (Linux/Windows/macOS), sfizz 1.2.3 (Windows/macOS binaries, Linux built from the source release), VSCO 2 CE Upright Piano and Violin Section (CC0) with generated `.sfz` mappings, 26 Surge factory patches mapped to compose roles. md5/sha256/git-sha1 verification, refuses Program Files, Windows kit always inside the app folder. Licences in `THIRD_PARTY.md`.
- **Front ends**: CLI (positional forms, `why:` lines), JSON-RPC (all of the above plus `cli.exec`), MCP (listen, compose, arrange, fix_clash, clash_report, variations, humanize, preview, instrument_add, lib_*, kit_status; `edit`/`batch` accept `reason`), GUI AI/Listen panel (command box, quick buttons, listening report, reasons in history).
- **Demo**: `examples/demo/build_demo_ai.pdaw` (compose song, listen, clash report, fix clash ×2, variations, humanize, listen), rendered with the kit to `examples/demo/AIDAW_AI_Demo.mp3`.

Known gaps: chord/key detection gets confused on dense synth mixes (e.g. hears the iv chord as home); Surge XT renders are not bit-deterministic; Windows/macOS kit installs untested; mac Surge download is md5-only; auditioning a `.vstpreset` needs its VST3 installed; `.vital`/`.h2p`/`.nmsv`/VST2 `.fxp` are index-only; whole-song `listen` and `fix clash` are slow (tens of seconds), so fix/variations default to the loop.
