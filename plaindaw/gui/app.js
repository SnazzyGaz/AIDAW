// PlainDAW browser GUI: a thin view over the same JSON-RPC op layer the CLI and AI use.
"use strict";
const PX = 28;            // pixels per quarter note in the arrangement
const ROW_KEY_H = 12;     // piano roll row height
const ROLL_PX = 48;       // piano roll pixels per quarter note
let S = null;             // last project.layout result
let ROLL = null;          // geometry of the drawn piano roll (for the live overlay)
let selPattern = null, selClip = null, selTrack = null;
let rpcId = 0;

async function rpc(method, params = {}) {
  const r = await fetch("/rpc", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++rpcId, method, params: { _as: "human", ...params } }) });
  const j = await r.json();
  if (j.error) { status("⚠ " + j.error.message, true); throw new Error(j.error.message); }
  return j.result;
}
function status(t, err) { const s = document.getElementById("status"); s.textContent = t; s.style.color = err ? "var(--warn)" : ""; }
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
const NOTE = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
const noteName = k => NOTE[k % 12] + (Math.floor(k / 12) - 1);

async function refresh() {
  S = await rpc("project.layout");
  const d = S.doc;
  document.getElementById("title").textContent = d.meta.title || "Untitled";
  const t0 = d.transport.tempo[0], ts = (d.transport.timeSig || [{ num: 4, den: 4 }])[0];
  document.getElementById("transport").textContent =
    `${t0.bpm} BPM · ${ts.num}/${ts.den}` + (d.transport.keySig ? ` · ${d.transport.keySig}` : "") +
    (d.transport.tempo.length > 1 ? ` · ${d.transport.tempo.length} tempo changes` : "");
  document.getElementById("undo").disabled = !S.canUndo;
  document.getElementById("redo").disabled = !S.canRedo;
  if (selPattern && !d.patterns[selPattern]) selPattern = null;
  drawTimeline(); drawHistory(); drawMixer(); drawRoll();
}

// ---------------------------------------------------------------- arrangement
function drawTimeline() {
  const tl = document.getElementById("timeline");
  tl.innerHTML = "";
  const width = 150 + S.endQn * PX;
  const ruler = el("div", "ruler"); ruler.style.width = width + "px";
  S.bars.forEach((q, i) => { const b = el("div", "bar", String(i + 1)); b.style.left = (150 + q * PX) + "px"; ruler.appendChild(b); });
  S.markers.forEach(m => { const b = el("div", "marker", m.name); b.style.left = (150 + m.qn * PX) + "px"; ruler.appendChild(b); });
  tl.appendChild(ruler);
  for (const t of S.doc.tracks) {
    if (t.type === "master") continue;
    const row = el("div", "row"); row.style.width = width + "px";
    const head = el("div", "head" + (selTrack === t.id ? " sel" : "")); head.appendChild(el("div", "", t.name));
    head.title = "click: the keyboard plays this track";
    head.onclick = () => selectTrack(t.id);
    const sub = t.type === "instrument" ? (t.instrument?.plugin || "") : t.type;
    head.appendChild(el("div", "sub", sub + (t.inserts?.length ? ` · ${t.inserts.length} fx` : "")));
    row.appendChild(head);
    const lane = el("div", "lane"); lane.style.width = (S.endQn * PX) + "px";
    S.bars.forEach(q => { const g = el("div", "gridline"); g.style.left = (q * PX) + "px"; lane.appendChild(g); });
    for (const c of t.clips || []) lane.appendChild(clipEl(t, c));
    row.appendChild(lane);
    tl.appendChild(row);
  }
  liveTimelineDecor(tl, ruler);
}

function clipEl(t, c) {
  const span = S.clips[c.id] || { start: 0, len: 4 };
  const isPat = !!c.pattern;
  const e = el("div", "clip" + (isPat ? "" : " audio") + (selClip === c.id ? " sel" : ""));
  const w = Math.max(6, span.len * PX);
  e.style.left = (span.start * PX) + "px"; e.style.width = w + "px";
  e.textContent = (isPat ? c.pattern : (c.file || "").split("/").pop()) + (c.repeat > 1 ? ` ×${c.repeat}` : "");
  e.title = `${c.id} @ ${c.at}`;
  if (isPat) {
    const p = S.doc.patterns[c.pattern];
    if (p && p.notes.length) {
      const cv = document.createElement("canvas"); cv.width = w; cv.height = 20; e.appendChild(cv);
      const g = cv.getContext("2d"); g.fillStyle = "#9fd3ff";
      const ks = p.notes.map(n => n.key), lo = Math.min(...ks), hi = Math.max(...ks), rng = Math.max(1, hi - lo);
      const reps = c.repeat || 1;
      for (let r = 0; r < reps; r++) for (const n of p.notes) {
        const x = (r * p.lengthBeats + n.t) * PX; if (x > w) break;
        g.fillRect(x, 17 - ((n.key - lo) / rng) * 15, Math.max(1, n.len * PX - 1), 2);
      }
    }
  }
  // drag to move (snap to beat); a click without movement selects
  e.addEventListener("mousedown", ev => {
    ev.preventDefault();
    const x0 = ev.clientX, left0 = span.start * PX; let moved = false;
    const mm = m => { const dx = m.clientX - x0; if (Math.abs(dx) > 3) moved = true; e.style.left = Math.max(0, left0 + dx) + "px"; };
    const mu = async m => {
      window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", mu);
      if (!moved) { selClip = c.id; if (isPat) selPattern = c.pattern; selectTrack(t.id, true); drawTimeline(); drawRoll(); return; }
      const by = Math.round((m.clientX - x0) / PX);
      if (by !== 0) { try { await rpc("clip.move", { track: t.id, clip: c.id, by }); status(`moved ${c.id} by ${by} beats`); } catch (_) {} }
      refresh();
    };
    window.addEventListener("mousemove", mm); window.addEventListener("mouseup", mu);
  });
  return e;
}

// ---------------------------------------------------------------- piano roll
function rollRange(p) {
  let lo = 48, hi = 72;
  for (const n of p.notes) { lo = Math.min(lo, n.key - 2); hi = Math.max(hi, n.key + 2); }
  return [Math.max(0, lo), Math.min(127, hi)];
}
function drawRoll() {
  const cv = document.getElementById("rollcanvas");
  const name = document.getElementById("rollname");
  const p = selPattern && S.doc.patterns[selPattern];
  if (!p) { ROLL = null; name.textContent = "(click a pattern clip)"; cv.width = 600; cv.height = 60;
    const g = cv.getContext("2d"); g.fillStyle = "#8a93a3"; g.font = "13px system-ui"; g.fillText("No pattern selected", 12, 34); return; }
  name.textContent = `${selPattern} · ${p.lengthBeats} beats · ${p.notes.length} notes`;
  const [lo, hi] = rollRange(p); const rows = hi - lo + 1;
  cv.width = 44 + p.lengthBeats * ROLL_PX; cv.height = rows * ROW_KEY_H;
  const g = cv.getContext("2d");
  for (let k = lo; k <= hi; k++) {
    const y = (hi - k) * ROW_KEY_H, black = [1,3,6,8,10].includes(k % 12);
    g.fillStyle = black ? "#171a1f" : "#1f232a"; g.fillRect(44, y, cv.width, ROW_KEY_H);
    g.fillStyle = black ? "#333" : "#ddd"; g.fillRect(0, y, 40, ROW_KEY_H - 1);
    if (k % 12 === 0) { g.fillStyle = "#555"; g.font = "9px system-ui"; g.fillText(noteName(k), 4, y + 10); }
  }
  const grid = parseFloat(document.getElementById("grid").value);
  for (let q = 0; q <= p.lengthBeats + 1e-9; q += grid) {
    const x = 44 + q * ROLL_PX; g.fillStyle = Math.abs(q % 4) < 1e-9 ? "#4a5263" : (Math.abs(q % 1) < 1e-9 ? "#323946" : "#262b34");
    g.fillRect(x, 0, 1, cv.height);
  }
  ROLL = { lo, hi, len: p.lengthBeats, w: cv.width, h: cv.height };
  p.notes.forEach(n => {
    if (n.key < lo || n.key > hi) return;
    const x = 44 + n.t * ROLL_PX, y = (hi - n.key) * ROW_KEY_H;
    g.fillStyle = `hsl(205, 80%, ${35 + (n.vel / 127) * 30}%)`;
    g.fillRect(x + 1, y + 1, Math.max(3, n.len * ROLL_PX - 2), ROW_KEY_H - 2);
  });
}
document.getElementById("rollcanvas").addEventListener("click", async ev => {
  const p = selPattern && S.doc.patterns[selPattern]; if (!p) return;
  const r = ev.target.getBoundingClientRect();
  const x = ev.clientX - r.left - 44, y = ev.clientY - r.top; if (x < 0) return;   // key column = preview (see live.js part)
  const [lo, hi] = rollRange(p);
  const key = hi - Math.floor(y / ROW_KEY_H), q = x / ROLL_PX;
  const hit = p.notes.findIndex(n => n.key === key && q >= n.t && q < n.t + n.len);
  try {
    if (hit >= 0) { await rpc("pattern.remove_notes", { pattern: selPattern, indices: [hit] }); status(`removed ${noteName(key)}`); }
    else {
      const grid = parseFloat(document.getElementById("grid").value), len = parseFloat(document.getElementById("nlen").value);
      const t = Math.floor(q / grid) * grid; if (t >= p.lengthBeats) return;
      await rpc("pattern.add_notes", { pattern: selPattern, notes: [{ t, len, key, vel: 100 }] }); status(`added ${noteName(key)} at beat ${t}`);
    }
  } catch (_) {}
  refresh();
});
document.getElementById("grid").addEventListener("change", drawRoll);

// ---------------------------------------------------------------- mixer
function drawMixer() {
  const box = document.getElementById("strips"); box.innerHTML = "";
  for (const t of S.doc.tracks) {
    const s = el("div", "strip" + (t.type === "master" ? " master" : ""));
    s.appendChild(el("div", "name", t.name));
    s.appendChild(el("div", "ins", (t.inserts || []).map(p => p.plugin.split(":")[1] + (p.bypass ? " (off)" : "")).join(", ")));
    const f = el("input", "fader"); f.type = "range"; f.min = -60; f.max = 6; f.step = 0.5; f.value = t.mix.gainDb ?? 0;
    const val = el("div", "val", `${(t.mix.gainDb ?? 0).toFixed(1)} dB`);
    f.addEventListener("input", () => val.textContent = `${parseFloat(f.value).toFixed(1)} dB`);
    f.addEventListener("change", () => rpc("mix.set", { track: t.id, gainDb: parseFloat(f.value) }).then(refresh, refresh));
    s.appendChild(f); s.appendChild(val);
    const pan = el("input", "pan"); pan.type = "range"; pan.min = -1; pan.max = 1; pan.step = 0.05; pan.value = t.mix.pan ?? 0;
    pan.title = "pan"; pan.addEventListener("change", () => rpc("mix.set", { track: t.id, pan: parseFloat(pan.value) }).then(refresh, refresh));
    pan.addEventListener("dblclick", () => rpc("mix.set", { track: t.id, pan: 0 }).then(refresh, refresh));
    s.appendChild(pan);
    if (t.type !== "master") {
      const b = el("div", "btns");
      const m = el("button", t.mix.mute ? "on" : "", "M"); m.title = "mute";
      m.onclick = () => rpc("mix.set", { track: t.id, mute: !t.mix.mute }).then(refresh, refresh);
      const so = el("button", t.mix.solo ? "on" : "", "S"); so.title = "solo";
      so.onclick = () => rpc("mix.set", { track: t.id, solo: !t.mix.solo }).then(refresh, refresh);
      b.appendChild(m); b.appendChild(so); s.appendChild(b);
    }
    box.appendChild(s);
  }
}

// ---------------------------------------------------------------- history
function drawHistory() {
  const ol = document.getElementById("history"); ol.innerHTML = "";
  for (const e of [...S.history].reverse()) {
    const li = el("li", e.state === "undone" ? "undone" : "");
    const who = el("span", "who" + (e.author === "ai" ? " ai" : ""), e.author);
    li.appendChild(who); li.appendChild(document.createTextNode(` #${e.n} ${e.op}`));
    if (e.reason) li.appendChild(el("span", "why", "why: " + e.reason));
    li.appendChild(el("span", "sum", (e.summary || []).join("; ")));
    if (e.kind === "op" && e.state === "applied") {
      li.title = "click to revert this edit";
      li.onclick = async () => { if (confirm(`Revert #${e.n} ${e.op}?`)) { try { await rpc("history.revert", { n: e.n }); } catch (_) {} refresh(); } };
    }
    ol.appendChild(li);
  }
}

// ---------------------------------------------------------------- render
function fmtAnalysis(a) {
  const m = a.master, lines = [
    `Loudness   ${m.integratedLufs} LUFS  (short-term max ${m.shortTermMaxLufs})`,
    `True peak  ${m.truePeakDbtp} dBTP   LRA ${m.lra} LU`,
    `Crest      ${m.crestDb} dB    width ${m.stereoWidth}`,
    `Low end    corr ${m.lowEndCorrelation}`, "", "Bands (dB rel. total)"];
  for (const [k, v] of Object.entries(a.bandsDbRel)) lines.push(`  ${k.split("_")[0].padEnd(9)} ${String(v).padStart(6)}`);
  if (a.sections?.length) { lines.push("", "Sections"); a.sections.forEach(s => lines.push(`  ${s.name.padEnd(10)} ${s.integratedLufs} LUFS`)); }
  if (a.tracks) { lines.push("", "Tracks (LUFS)"); for (const [k, v] of Object.entries(a.tracks)) lines.push(`  ${k.padEnd(10)} ${v.integratedLufs ?? "silent"}`); }
  if (a.warnings?.length) { lines.push("", "Warnings"); a.warnings.forEach(w => lines.push("  • " + w)); }
  return lines.join("\n");
}
document.getElementById("render").onclick = async () => {
  const b = document.getElementById("render"); b.disabled = true; status("rendering…");
  try {
    const r = await rpc("render", { formats: "wav,mp3", name: "gui-render" });
    status(`rendered ${r.seconds}s`);
    const mp3 = r.files.find(f => f.path.endsWith(".mp3")) || r.files[0];
    const p = document.getElementById("player"); p.src = "/renders/" + mp3.path.replace(/\\/g, "/").split("/renders/").pop() + "?t=" + Date.now();
    document.getElementById("analysis").textContent = fmtAnalysis(r.analysis);
  } catch (_) {}
  b.disabled = false;
};
document.getElementById("undo").onclick = () => rpc("history.undo").then(r => { status(`undid #${r.undid} ${r.op}`); refresh(); }, refresh);
document.getElementById("redo").onclick = () => rpc("history.redo").then(r => { status(`redid #${r.redid} ${r.op}`); refresh(); }, refresh);
window.addEventListener("keydown", ev => {
  if (!(ev.ctrlKey || ev.metaKey)) return;
  if (ev.key === "z" && !ev.shiftKey) { ev.preventDefault(); document.getElementById("undo").click(); }
  if (ev.key === "y" || (ev.key === "z" && ev.shiftKey)) { ev.preventDefault(); document.getElementById("redo").click(); }
});

// ---------------------------------------------------------------- AI / listen panel
const QUICK = [
  ["Listen", "listen"], ["Clash report", "clash report"], ["Fix kick/bass", "fix clash kick bass"],
  ["Compose song", "compose song --genre house --seed 1"], ["New lead ×4", "variations 4 compose lead --target keyfit"],
  ["Humanize hats", "humanize hats --feel natural"], ["Preview loop", "preview"], ["Kit status", "kit status"],
];
async function runCli(line) {
  const out = document.getElementById("aiout"), btn = document.getElementById("airun");
  out.classList.remove("err"); out.textContent = `> ${line}\n…`; btn.disabled = true; status("working…");
  try {
    const r = await rpc("cli.exec", { line, _as: "human" });
    out.textContent = `> ${line}\n${r.text}`;
    if (r.method === "listen") document.getElementById("listen").textContent = r.text;
    if (r.method === "preview" && r.result?.path) {
      const p = document.getElementById("player"); p.src = "/renders/" + r.result.path.replace(/\\/g, "/").split("/renders/").pop() + "?t=" + Date.now(); p.play().catch(() => {});
    }
    status(`ran ${r.method}`);
  } catch (e) { out.classList.add("err"); out.textContent = `> ${line}\n${e.message}`; }
  btn.disabled = false;
  refresh();
}
document.getElementById("aiform").addEventListener("submit", ev => {
  ev.preventDefault(); const v = document.getElementById("aicmd").value.trim(); if (v) runCli(v);
});
for (const [label, line] of QUICK) {
  const b = el("button", "", label); b.title = line; b.type = "button";
  b.onclick = () => { document.getElementById("aicmd").value = line; runCli(line); };
  document.getElementById("aiquick").appendChild(b);
}

// live updates: edits from the CLI, the AI (MCP) or a text editor show up here
let pending = null;
function live() {
  const es = new EventSource("/events");
  es.onmessage = ev => { const e = JSON.parse(ev.data); if (e.type === "hello") return;
    if (e.version !== S?.version) { clearTimeout(pending); pending = setTimeout(refresh, 80); } };
  es.onerror = () => { es.close(); setTimeout(live, 2000); };
}
refresh().then(() => { live(); liveInit(); });


// ================================================================ live playback + live MIDI
// The server renders the timeline ahead in the background (only changed tracks are re-rendered)
// and streams it to the audio device; live notes are synthesised in real time.
let TS = null, TS_AT = 0, polling = null, octave = 0;
const heldKeys = new Map();

async function lrpc(method, params = {}) {   // quiet rpc for the transport (no status spam)
  const r = await fetch("/rpc", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++rpcId, method, params: { _as: "human", ...params } }) });
  const j = await r.json();
  if (j.error) throw new Error(j.error.message);
  return j.result;
}
function setTS(st) { if (st && st.posQn !== undefined) { TS = st; TS_AT = performance.now(); paintTransport(); } return st; }
async function poll() {
  clearTimeout(polling);
  try { setTS(await lrpc("transport.status")); } catch (_) {}
  polling = setTimeout(poll, TS && (TS.playing || TS.render.state === "rendering" || TS.active.length) ? 120 : 600);
}
function nowQn() {
  if (!TS) return 0;
  let q = TS.posQn;
  if (TS.playing && TS.render.state !== "error") q += (performance.now() - TS_AT) / 1000 * TS.qnPerSec;
  const lr = TS.loopRange;
  if (TS.playing && TS.loop) {
    const s = lr ? lr.start : 0, e = lr ? lr.end : (S ? S.endQn - 8 : 16);
    if (e > s && q >= e) q = s + ((q - e) % (e - s));
  }
  return q;
}
function fmtPos(q) {
  const bars = S ? S.bars : []; let b = 0;
  while (b + 1 < bars.length && bars[b + 1] <= q + 1e-9) b++;
  const rel = q - (bars[b] || 0), beat = Math.floor(rel + 1e-9), six = Math.floor((rel - beat) * 4 + 1e-9);
  return `${b + 1}.${beat + 1}.${six + 1}`;
}
function paintTransport() {
  if (!TS) return;
  const on = (id, v) => document.getElementById(id).classList.toggle("on", !!v);
  on("t-play", TS.playing); on("t-loop", TS.loop); on("t-click", TS.metronome); on("t-rec", TS.record);
  document.getElementById("t-play").innerHTML = TS.playing ? "&#10074;&#10074; Pause" : "&#9654; Play";
  const r = TS.render || {};
  const rs = r.state === "rendering" ? "rendering…" : r.state === "error" ? "render error" :
    r.renderSec !== undefined ? `ready (${r.renderSec}s, ${r.cachedParts} cached)` : r.state;
  document.getElementById("t-info").textContent =
    `${rs} · ${TS.output === "portaudio" ? `${TS.latencyMs} ms` : TS.output} · cpu ${TS.cpu}%` + (TS.xruns ? ` · ${TS.xruns} dropouts` : "");
  document.getElementById("t-info").title = (TS.error || "") + (r.warnings && r.warnings.length ? "\n" + r.warnings.join("\n") : "")
    + `\n${TS.sampleRate} Hz, buffer ${TS.blocksize}, device ${TS.device ?? "default"}`;
  const lz = document.getElementById("loopzone");
  if (lz) lz.classList.toggle("off", !TS.loop);
}
function liveTimelineDecor(tl, ruler) {
  const ph = el("div", "playhead"); ph.id = "playhead"; tl.appendChild(ph);
  const lr = S.doc.transport.loop;
  if (lr) {
    const s = S.bars.length ? qnOf(lr.start) : 0, e = qnOf(lr.end);
    const z = el("div", "loopzone" + (TS && !TS.loop ? " off" : "")); z.id = "loopzone";
    z.style.left = (150 + s * PX) + "px"; z.style.width = Math.max(2, (e - s) * PX) + "px"; ruler.appendChild(z);
  }
  // click the ruler = move the playhead; drag = set the loop range (snaps to bars)
  ruler.onmousedown = ev => {
    const r0 = ruler.getBoundingClientRect(), x0 = ev.clientX;
    const qAt = x => Math.max(0, (x - r0.left - 150) / PX);
    const up = async m => {
      window.removeEventListener("mouseup", up);
      if (Math.abs(m.clientX - x0) < 4) { setTS(await lrpc("transport.seek", { at: Math.round(qAt(x0) * 4) / 4 })); return; }
      const snap = q => { let best = 0; for (const b of S.bars) if (Math.abs(b - q) < Math.abs(best - q)) best = b; return best; };
      let a = snap(qAt(Math.min(x0, m.clientX))), b = snap(qAt(Math.max(x0, m.clientX)));
      if (b <= a) return;
      try { setTS(await lrpc("transport.loop", { on: true, start: fmtPos(a), end: fmtPos(b) })); status(`loop ${fmtPos(a)} – ${fmtPos(b)}`); } catch (e) { status("⚠ " + e.message, true); }
      refresh();
    };
    window.addEventListener("mouseup", up);
  };
}
function qnOf(pos) {   // 'bar.beat.16th' -> quarter notes using the drawn bar lines (4/4 beats)
  if (typeof pos === "number") return pos;
  const [b, be = 1, s = 1] = String(pos).split(".").map(Number);
  return (S.bars[b - 1] ?? (b - 1) * 4) + (be - 1) + (s - 1) * 0.25;
}
function frame() {
  if (S && TS) {
    const q = nowQn();
    document.getElementById("t-pos").textContent = fmtPos(q);
    const ph = document.getElementById("playhead"), tl = document.getElementById("timeline");
    if (ph) {
      ph.style.left = (150 + q * PX) + "px"; ph.style.height = tl.scrollHeight + "px";
      if (TS.playing) {   // keep the playhead in view
        const x = 150 + q * PX;
        if (x > tl.scrollLeft + tl.clientWidth - 40 || x < tl.scrollLeft + 150) tl.scrollLeft = Math.max(0, x - 200);
      }
    }
    drawRollOverlay(q);
  }
  requestAnimationFrame(frame);
}
function drawRollOverlay(q) {
  const ov = document.getElementById("rolloverlay"), cv = document.getElementById("rollcanvas");
  if (ov.width !== cv.width || ov.height !== cv.height) { ov.width = cv.width; ov.height = cv.height; }
  const g = ov.getContext("2d"); g.clearRect(0, 0, ov.width, ov.height);
  if (!ROLL || !TS) return;
  // incoming / held notes: light up their keys
  const tid = selTrack;
  for (const a of TS.active) {
    if (tid && a.track !== tid) continue;
    if (a.key < ROLL.lo || a.key > ROLL.hi) continue;
    const y = (ROLL.hi - a.key) * ROW_KEY_H;
    g.fillStyle = "rgba(255,90,90,.85)"; g.fillRect(0, y, 40, ROW_KEY_H - 1);
    g.fillStyle = "rgba(255,90,90,.12)"; g.fillRect(44, y, ROLL.w, ROW_KEY_H - 1);
  }
  for (const k of heldKeys.keys()) {
    if (k < ROLL.lo || k > ROLL.hi) continue;
    g.fillStyle = "rgba(255,200,90,.8)"; g.fillRect(0, (ROLL.hi - k) * ROW_KEY_H, 40, ROW_KEY_H - 1);
  }
  // playhead inside the selected clip's pattern
  const span = selClip && S.clips[selClip];
  if (span && q >= span.start && q < span.start + span.len) {
    const rel = (q - span.start) % ROLL.len, x = 44 + rel * ROLL_PX;
    g.fillStyle = "#ff5a5a"; g.fillRect(x, 0, 2, ROLL.h);
  }
}
function selectTrack(id, quiet) {
  selTrack = id;
  const t = S && S.doc.tracks.find(x => x.id === id);
  document.getElementById("m-target").textContent = `Keyboard plays: ${t ? t.name : "(click a track)"}` +
    (t && t.instrument && !String(t.instrument.plugin).startsWith("builtin:") ? " (preview uses a stand-in synth)" : "");
  lrpc("midi.target", { track: id }).then(setTS, () => {});
  if (!quiet) drawTimeline();
}
async function noteOn(key, vel = 100) {
  if (heldKeys.has(key)) return;
  heldKeys.set(key, true);
  const track = selTrack || (selPattern && S.doc.tracks.find(t => (t.clips || []).some(c => c.pattern === selPattern))?.id);
  try { const r = await lrpc("live.note", { track, key, vel, on: true }); if (r.standIn) status("plugin track: live preview uses a stand-in synth"); } catch (e) { status("⚠ " + e.message, true); }
  poll();
}
async function noteOff(key) {
  if (!heldKeys.has(key)) return;
  heldKeys.delete(key);
  const track = selTrack || (selPattern && S.doc.tracks.find(t => (t.clips || []).some(c => c.pattern === selPattern))?.id);
  try { await lrpc("live.note", { track, key, on: false }); } catch (_) {}
}
// piano-roll key column: press = hear the note
document.getElementById("rollcanvas").addEventListener("mousedown", ev => {
  if (!ROLL) return;
  const r = ev.target.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
  if (x >= 44) return;
  const key = ROLL.hi - Math.floor(y / ROW_KEY_H);
  noteOn(key, 100);
  const up = () => { window.removeEventListener("mouseup", up); noteOff(key); };
  window.addEventListener("mouseup", up);
});
// computer keyboard as a MIDI keyboard (when not typing in a box)
const KEYMAP = { a: 0, w: 1, s: 2, e: 3, d: 4, f: 5, t: 6, g: 7, y: 8, h: 9, u: 10, j: 11, k: 12, o: 13, l: 14 };
function typing(ev) { const t = ev.target; return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT"); }
window.addEventListener("keydown", ev => {
  if (typing(ev) || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const k = ev.key.toLowerCase();
  if (k === " ") { ev.preventDefault(); document.getElementById("t-play").click(); return; }
  if (ev.key === "Home") { document.getElementById("t-home").click(); return; }
  if (k === "z") { octave = Math.max(-3, octave - 1); status(`octave ${4 + octave}`); return; }
  if (k === "x") { octave = Math.min(4, octave + 1); status(`octave ${4 + octave}`); return; }
  if (k === "m" && !ev.repeat) { document.getElementById("t-click").click(); return; }
  if (k === "r" && !ev.repeat) { document.getElementById("t-rec").click(); return; }
  if (k === "q" && !ev.repeat) { document.getElementById("t-loop").click(); return; }
  if (k in KEYMAP && !ev.repeat && !keyNote.has(k)) { const key = 60 + 12 * octave + KEYMAP[k]; keyNote.set(k, key); noteOn(key, 100); }
});
const keyNote = new Map();
window.addEventListener("keyup", ev => {
  const k = ev.key.toLowerCase();
  if (keyNote.has(k)) { noteOff(keyNote.get(k)); keyNote.delete(k); }
});
window.addEventListener("blur", () => { for (const [k, key] of keyNote) noteOff(key); keyNote.clear(); });
async function liveInit() {
  const $ = id => document.getElementById(id);
  $("t-play").onclick = async () => {
    try { setTS(await lrpc(TS && TS.playing ? "transport.stop" : "transport.play")); }
    catch (e) { status("⚠ " + e.message, true); }
    poll();
  };
  $("t-stop").onclick = async () => { try { setTS(await lrpc("transport.stop", { rewind: true })); } catch (_) {} poll(); refresh(); };
  $("t-home").onclick = async () => { try { setTS(await lrpc("transport.seek", { at: TS && TS.loop && TS.loopRange ? TS.loopRange.start : 0 })); } catch (_) {} };
  $("t-loop").onclick = async () => { try { setTS(await lrpc("transport.loop", { on: !(TS && TS.loop) })); } catch (_) {} };
  $("t-click").onclick = async () => { try { setTS(await lrpc("transport.metronome", { on: !(TS && TS.metronome) })); } catch (_) {} };
  $("t-rec").onclick = async () => {
    const on = !(TS && TS.record);
    try { setTS(await lrpc("transport.record", { on, track: selTrack, quantize: parseFloat($("m-quant").value) }));
      status(on ? `recording into ${selTrack || "the first instrument track"} — press Play` : "recording off"); } catch (e) { status("⚠ " + e.message, true); }
    if (!on) refresh();
  };
  $("m-quant").onchange = () => lrpc("transport.record", { on: !!(TS && TS.record), quantize: parseFloat($("m-quant").value) }).then(setTS, () => {});
  // audio devices / MIDI ports
  try {
    const d = await lrpc("audio.devices"), sel = $("a-dev");
    for (const x of d.devices || []) { const o = el("option", "", `${x.name} (${x.hostapi})`); o.value = String(x.id); sel.appendChild(o); }
    if (d.current !== null && d.current !== undefined) sel.value = String(d.current);
    if (d.error) sel.title = d.error;
  } catch (_) {}
  $("a-dev").onchange = async () => { try { setTS(await lrpc("audio.set", { device: $("a-dev").value })); status("audio output changed"); } catch (e) { status("⚠ " + e.message, true); } };
  $("a-buf").onchange = async () => { try { setTS(await lrpc("audio.set", { blocksize: parseInt($("a-buf").value) })); status(`buffer ${$("a-buf").value}`); } catch (e) { status("⚠ " + e.message, true); } };
  try {
    const m = await lrpc("midi.ports"), sel = $("m-port");
    for (const n of m.inputs || []) { const o = el("option", "", n); o.value = n; sel.appendChild(o); }
    if (!m.available) { sel.disabled = true; sel.title = m.error || "no MIDI support"; }
  } catch (_) {}
  $("m-port").onchange = async () => {
    const v = $("m-port").value;
    try { setTS(await lrpc(v ? "midi.open" : "midi.close", v ? { port: v, track: selTrack } : {})); status(v ? `MIDI: ${v}` : "MIDI closed"); } catch (e) { status("⚠ " + e.message, true); }
  };
  const st = await lrpc("transport.status").catch(() => null);
  setTS(st);
  if (st) { $("a-buf").value = String(st.blocksize); if (st.midiTrack) selectTrack(st.midiTrack, true); }
  if (!selTrack && S) { const t = S.doc.tracks.find(x => x.type === "instrument"); if (t) selectTrack(t.id); }
  poll(); requestAnimationFrame(frame);
}
