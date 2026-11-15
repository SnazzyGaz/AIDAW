// PlainDAW browser GUI: a thin view over the same JSON-RPC op layer the CLI and AI use.
"use strict";
const PX = 28;            // pixels per quarter note in the arrangement
const ROW_KEY_H = 12;     // piano roll row height
const ROLL_PX = 48;       // piano roll pixels per quarter note
let S = null;             // last project.layout result
let selPattern = null, selClip = null;
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
    const head = el("div", "head"); head.appendChild(el("div", "", t.name));
    const sub = t.type === "instrument" ? (t.instrument?.plugin || "") : t.type;
    head.appendChild(el("div", "sub", sub + (t.inserts?.length ? ` · ${t.inserts.length} fx` : "")));
    row.appendChild(head);
    const lane = el("div", "lane"); lane.style.width = (S.endQn * PX) + "px";
    S.bars.forEach(q => { const g = el("div", "gridline"); g.style.left = (q * PX) + "px"; lane.appendChild(g); });
    for (const c of t.clips || []) lane.appendChild(clipEl(t, c));
    row.appendChild(lane);
    tl.appendChild(row);
  }
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
      if (!moved) { selClip = c.id; if (isPat) selPattern = c.pattern; drawTimeline(); drawRoll(); return; }
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
  if (!p) { name.textContent = "(click a pattern clip)"; cv.width = 600; cv.height = 60;
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
  const x = ev.clientX - r.left - 44, y = ev.clientY - r.top; if (x < 0) return;
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
    const p = document.getElementById("player"); p.src = "/renders/" + mp3.path.split("/renders/").pop() + "?t=" + Date.now();
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
      const p = document.getElementById("player"); p.src = "/renders/" + r.result.path.split("/renders/").pop() + "?t=" + Date.now(); p.play().catch(() => {});
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
refresh().then(live);
