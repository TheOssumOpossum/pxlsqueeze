"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = {
  data: null,
  filter: "attention",
  selected: null,
  renderedKey: null, // id + output rev: when it changes the players are rebuilt
  playing: false,
  dragging: null,
  compareT: null,
};

// ------------------------------------------------------------------ formatting
function bytes(n) {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return i === 0 ? `${n} B` : `${n.toFixed(1)} ${u[i]}`;
}
function secs(t) {
  if (t == null || isNaN(t)) return "–";
  t = Math.max(0, t);
  const m = Math.floor(t / 60);
  const s = (t - m * 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
}
function pct(x) { return `${Math.round(x * 100)}%`; }

// ------------------------------------------------------------------ api
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  let j = null;
  try { j = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((j && j.detail) || `Request failed (${r.status})`);
  return j;
}

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("bad", bad);
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), bad ? 6000 : 2500);
}

// ------------------------------------------------------------------ item helpers
function item(id) { return S.data?.items.find((i) => i.id === id); }

function needsAttention(it) {
  if (it.status === "finalized" || it.decision) return false;
  return it.flags.length > 0 || it.status === "verified" || it.needs_encode || it.status === "error";
}

// The background job for a video, if it's running or waiting in line.
function jobFor(it) {
  const cur = S.data.jobs.current;
  if (cur && cur.id === it.id) return { ...cur, active: true };
  const i = S.data.jobs.queued.findIndex((j) => j.id === it.id);
  return i < 0 ? null : { ...S.data.jobs.queued[i], active: false, position: i + 1 };
}

function ordinal(n) {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

function summaryChips(it) {
  const c = [];
  const e = it.effective;
  const job = jobFor(it);
  if (job && job.active) {
    c.push(job.kind === "verify" ? ["Checking", "busy"] : [`Encoding ${pct(job.progress)}`, "busy", "", job.progress]);
  } else if (job) {
    c.push([job.kind === "verify" ? "Queued for check" : "Queued", "busy",
      job.position === 1 ? "Next up" : `${ordinal(job.position)} in line`]);
  }
  if (it.status === "error" && !job) c.push(["Error", "bad"]);
  if (it.status === "finalized" && it.decision !== "trash") c.push(["Done", "good"]);
  if (it.decision === "approved" && it.status !== "finalized") c.push(["Approved", "good"]);
  if (it.decision === "rejected") c.push(["Rejected", "bad"]);
  if (it.decision === "trash") return [[it.status === "finalized" ? "Trashed" : "Marked for Trash", "bad"]];
  if (it.flags.includes("short")) c.push([`Short clip (${(it.probe?.duration || 0).toFixed(1)}s)`, "warn"]);
  if (e.rotation) c.push([`Rotated ${e.rotation === 270 ? "90° left" : e.rotation === 90 ? "90° right" : "180°"}`, ""]);
  if (e.trimmed) c.push([`Trimmed ${((it.probe?.duration || 0) - (e.end - e.start)).toFixed(1)}s`, ""]);
  if (it.flags.includes("rotation-unsure")) c.push(["Check rotation", "warn"]);
  if (it.flags.includes("quality")) c.push(["Check quality", "warn"]);
  if (it.flags.includes("hdr10plus")) c.push(["HDR10+ lost", "warn"]);
  if (it.needs_encode && it.status !== "error" && !job) c.push([it.encoded_with ? "Changes not encoded" : "Not encoded yet", "warn"]);
  if (it.status === "skipped" && !job) c.push(["Kept original", ""]);
  return c;
}

function subline(it) {
  const p = it.probe || {};
  const enc = it.encode || {};
  if (it.status === "pending") return "Waiting for analysis";
  if (enc.size && ["verified", "encoded", "finalized"].includes(it.status))
    return `${secs(p.duration)}  ${bytes(p.size)} to ${bytes(enc.size)} (${pct(1 - enc.ratio)} smaller)`;
  return `${secs(p.duration)}  ${bytes(p.size)}`;
}

// ------------------------------------------------------------------ header + list
function renderHeader() {
  const d = S.data;
  $("#root").textContent = d.root;
  $("#ver").textContent = d.version;
  const sm = d.summary;
  const stat = (n, label, tip, cls = "") => `<span class="stat ${cls}" title="${esc(tip)}"><b>${n}</b> ${label}</span>`;
  const parts = [
    stat(sm.total, sm.total === 1 ? "video" : "videos", "PXL videos in this folder"),
    stat(sm.encoded, "encoded", "Have a new file: waiting for your decision, approved, or already finalized"),
  ];
  if (sm.trashed) parts.push(stat(sm.trashed, "trashed", "Marked for the Trash or already moved there"));
  if (sm.kept) parts.push(stat(sm.kept, "kept as is", "Original kept: you rejected the new file, or it didn't save enough space"));
  parts.push(stat(sm.todo, "to go", "Still need encoding, have changes that aren't encoded yet, or failed", sm.todo ? "" : "muted"));
  const space = [stat(bytes(sm.freed_bytes), "freed", "Space already freed by finalizing")];
  if (sm.pending_bytes) space.push(stat(`+${bytes(sm.pending_bytes)}`, "pending",
    "More space you'll free once the encoded and trashed videos are finalized"));
  $("#totals").innerHTML = `<span class="stats">${parts.join("")}</span><span class="stats space">${space.join("")}</span>`;
  const t = d.totals;

  const cur = d.jobs.current;
  const q = d.jobs.queued.length;
  $("#job").innerHTML = cur
    ? `<span>${cur.kind === "verify" ? "Checking" : "Encoding"} ${esc(cur.id)}</span><span class="bar"><i style="width:${pct(cur.progress)}"></i></span><span>${pct(cur.progress)}${q ? `, ${q} queued` : ""}</span>`
    : "";

  const ea = $("#encode-all");
  const pend = t.pending_encode - q - (cur ? 1 : 0);
  ea.hidden = pend <= 0;
  ea.textContent = `Encode ${pend} video${pend === 1 ? "" : "s"}`;
  ea.title = "Encodes every video that has no new file yet, has unencoded changes, or failed last time.";

  const shortN = d.items.filter((i) => i.flags.includes("short")).length;
  const tb = $("#trash-short");
  tb.hidden = shortN === 0;
  tb.textContent = `Mark ${shortN} short clip${shortN === 1 ? "" : "s"} for Trash`;
  tb.title = `Clips ${d.settings.short_clip_seconds}s or shorter. You can undo this per video before finalizing.`;

  const f = d.finalize;
  const fb = $("#finalize");
  fb.hidden = f.count === 0;
  fb.textContent = `Move ${f.count} original${f.count === 1 ? "" : "s"} to Trash`;
}

// Encoded, checked and waiting for a decision: nothing left to do but approve or reject.
function readyToDecide(it) {
  return it.status === "verified" && !it.decision && !it.needs_encode && !it.stale;
}

const FILTERS = {
  attention: needsAttention,
  ready: readyToDecide,
  all: () => true,
  approved: (i) => i.decision === "approved",
  trash: (i) => i.decision === "trash",
};

function visibleItems() {
  return S.data.items.filter(FILTERS[S.filter] || FILTERS.all);
}

function renderList() {
  document.querySelectorAll(".tab").forEach((b) => {
    b.setAttribute("aria-selected", String(b.dataset.filter === S.filter));
    const n = S.data.items.filter(FILTERS[b.dataset.filter]).length;
    $(".count", b).textContent = n ? String(n) : "";
  });
  const items = visibleItems();
  const ul = $("#list");
  if (!items.length) {
    const msg = { attention: "Nothing needs a look right now.",
      ready: "Nothing is waiting for a decision. Videos show up here once they're encoded and pass their check.",
      trash: "No videos are marked for the Trash. Press T on a video to mark it." }[S.filter] || "No videos here.";
    ul.innerHTML = `<li class="empty">${msg}</li>`;
    return;
  }
  ul.innerHTML = items.map((it) => {
    const cls = it.decision === "approved" ? "is-approved"
      : it.decision === "rejected" || it.decision === "trash" ? "is-rejected"
      : it.flags.length || it.status === "error" ? "is-attention" : "";
    const thumb = it.probe && it.source_exists ? `style="background-image:url('/thumb/${encodeURIComponent(it.id)}?r=${it.effective.rotation}')"` : "";
    const chips = summaryChips(it).map(([t, k, tip, prog]) =>
      `<span class="chip ${k}"${tip ? ` title="${esc(tip)}"` : ""}${prog != null ? ` style="--p:${pct(prog)}"` : ""}>${esc(t)}</span>`).join("");
    return `<li><button class="item ${cls}" data-id="${esc(it.id)}" aria-current="${it.id === S.selected}">
      <span class="thumb" ${thumb}></span>
      <span><div class="name">${esc(it.id)}</div><div class="sub">${esc(subline(it))}</div><div class="chips">${chips}</div></span>
    </button></li>`;
  }).join("");
}

// ------------------------------------------------------------------ detail
function statusSentence(it) {
  const cur = S.data.jobs.current;
  if (cur && cur.id === it.id) return ["", cur.kind === "verify" ? "Checking the new file now." : `Encoding now, ${pct(cur.progress)} done.`];
  const q = S.data.jobs.queued.find((j) => j.id === it.id);
  if (q) return ["", q.kind === "verify" ? "Queued for checking." : "Queued for encoding."];
  if (it.status === "error") return ["bad", `${it.error || "Something went wrong."}${it.needs_encode ? " Press Try again to encode it again." : ""}`];
  if (it.status === "finalized") return ["", it.decision === "trash" ? "Moved to the Trash. No copy was kept." : "Done. The original was moved to the Trash."];
  if (it.decision === "trash") return ["bad", "Marked for the Trash. The original and any new file go to the Trash when you finalize. Press U to keep it."];
  if (it.status === "pending") return ["", "Not analyzed yet. Run pxlsqueeze analyze on this folder."];
  if (it.decision === "rejected") return ["", "Rejected. The original is kept and the new file was deleted. Encode again if you change your mind."];
  if (it.status === "skipped" && !it.needs_encode) {
    const w = (it.warnings || []).find((x) => /^(Output is only|The new file is only|The new file came out|An output)/.test(x));
    return ["", w || "Skipped. The original is kept."];
  }
  if (it.flags.includes("short")) return ["attention", `This clip is only ${(it.probe?.duration || 0).toFixed(1)} seconds long. Press T to mark it for the Trash if you don't need it.`];
  if (it.needs_encode) return ["attention", it.encoded_with ? "Your changes aren't in the new file yet. Press Encode with changes to update it." : "Not encoded yet. Check rotation and trim, then press Encode."];
  if (it.decision === "approved") return ["", "Approved. The original will be moved to the Trash when you finalize."];
  if (it.flags.includes("quality")) return ["attention", "Quality score is lower than usual. Compare a few frames before approving."];
  if (it.flags.includes("rotation-unsure")) return ["attention", "Rotation couldn't be detected with confidence. Check that the picture is upright."];
  return ["", "Ready to approve."];
}

function qualityText(it) {
  const q = it.encode?.quality;
  if (!q) return "";
  const name = q.metric === "vmaf" ? "VMAF" : "SSIM";
  return `${name} ${q.mean} (lowest 1%: ${q.p1})`;
}

function rotationText(it) {
  const r = it.rotation || {};
  const eff = it.effective.rotation;
  const label = eff === 0 ? "No rotation" : eff === 90 ? "Rotate 90° right" : eff === 270 ? "Rotate 90° left" : "Rotate 180°";
  if (r.override != null) return `${label}, set by you`;
  if (!r.method || r.method === "none") return `${label}, nothing detected`;
  return `${label}, detected from ${r.method} (${pct(r.confidence || 0)} sure)`;
}

function trimText(it) {
  const t = it.trim || {};
  const e = it.effective;
  if (t.override) return `${secs(e.start)} to ${secs(e.end)}, set by you`;
  if (e.trimmed) return `${secs(e.start)} to ${secs(e.end)}, from speech`;
  if (t.is_talking) return "Talking detected, nothing to trim";
  return "Whole video kept";
}

function buildDetail(it) {
  const hasOut = it.has_output;
  const p = it.probe || {};
  const sizeNote = (w) => /^(Output is only|The new file is only|The new file came out)/.test(w);
  const notes = (it.warnings || []).filter((w) => !sizeNote(w) || it.status !== "skipped");
  $("#detail").innerHTML = `
    <div class="title-row"><h1>${esc(it.id)}</h1><span class="muted" id="d-facts"></span></div>
    <p class="status-line" id="d-status"></p>
    <ul class="notes">${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>

    <div class="stages">
      <section class="stage">
        <div class="stage-head"><span><b>Original</b> <span id="src-rot-note"></span> <span id="src-play-note"></span></span><span>${bytes(p.size)}</span></div>
        <div class="stage-box" id="box-src"><video id="v-src" playsinline preload="metadata"></video></div>
      </section>
      <section class="stage">
        <div class="stage-head"><span><b>New file</b> <span id="out-note"></span> <span id="out-play-note"></span></span><span>${hasOut ? bytes(it.encode?.size) : ""}</span></div>
        <div class="stage-box" id="box-out">
          ${hasOut ? `<video id="v-out" playsinline preload="metadata" muted></video>` : ""}
          <div class="veil" id="out-veil" ${hasOut ? "hidden" : ""}>${hasOut ? "" : "No new file yet."}</div>
        </div>
      </section>
    </div>

    <div class="transport">
      <button class="btn" id="play">Play</button>
      <span class="clock" id="clock"></span>
      <span class="muted" style="font-size:var(--fs-xs)">Sound plays from the original.</span>
    </div>
    <div class="timeline" id="tl" aria-label="Timeline">
      <div class="tl-track" id="tl-track"></div>
      <div class="tl-handle" id="h-start" tabindex="0" role="slider" aria-label="Trim start"></div>
      <div class="tl-handle" id="h-end" tabindex="0" role="slider" aria-label="Trim end"></div>
      <div class="tl-head" id="tl-head"></div>
      <div class="tl-ticks" id="tl-ticks"></div>
    </div>
    <div class="legend"><span><i style="background:var(--speech)"></i>Speech</span><span><i style="background:repeating-linear-gradient(135deg,#111 0 3px,#444 3px 6px)"></i>Cut from the new file</span></div>

    <div class="controls">
      <div class="group">
        <h2>Rotation <span id="rot-text"></span></h2>
        <div class="row">
          <button class="btn" data-rot="-90">Rotate left</button>
          <button class="btn" data-rot="90">Rotate right</button>
          <button class="btn" data-rot="180">Flip 180°</button>
          <button class="btn btn-quiet" data-rot="reset">Use detected</button>
        </div>
      </div>
      <div class="group">
        <h2>Trim <span id="trim-text"></span></h2>
        <div class="row">
          <button class="btn" data-trim="start">Start at playhead</button>
          <button class="btn" data-trim="end">End at playhead</button>
          <button class="btn btn-quiet" data-trim="reset">Use detected</button>
          <button class="btn btn-quiet" data-trim="none">Keep whole video</button>
        </div>
      </div>
    </div>

    <section class="compare">
      <h2>Frame check</h2>
      <div class="row" style="margin-bottom:8px">
        <button class="btn" id="cmp-go" ${hasOut ? "" : "disabled"}>Compare this frame</button>
        <button class="btn" id="cmp-actual" aria-pressed="false" hidden>Actual size</button>
        <span class="muted" id="cmp-info" style="font-size:var(--fs-xs);align-self:center">${hasOut ? "Pause on a detailed moment, then compare the full-resolution frames." : "Available once the new file exists."}</span>
      </div>
      <div id="cmp-area" hidden>
        <div class="wipe" id="wipe">
          <img id="cmp-out" alt="New file frame">
          <img id="cmp-src" class="over" alt="Original frame">
          <div class="seam" id="seam"></div>
        </div>
        <input type="range" class="wipe-range" id="wipe-range" min="0" max="100" value="50" aria-label="Wipe between original and new">
        <div class="wipe-labels"><span>Original on the left</span><span>New file on the right</span></div>
      </div>
    </section>

    <div class="decide">
      <span class="why" id="d-why"></span>
      <span class="keys"><kbd>A</kbd> approve <kbd>R</kbd> reject <kbd>T</kbd> trash <kbd>U</kbd> undo <kbd>J</kbd>/<kbd>K</kbd> next/prev <kbd>Space</kbd> play</span>
      <button class="btn btn-quiet" id="b-undo" hidden></button>
      <button class="btn" id="b-encode">Encode with changes</button>
      <button class="btn btn-danger" id="b-trash" title="Move the original, and any new file, to the Trash when you finalize">Trash video</button>
      <button class="btn btn-danger" id="b-reject" title="Keep the original and delete the new file">Reject</button>
      <button class="btn btn-primary" id="b-approve">Approve</button>
    </div>`;

  wirePlayers(it);
  wireTimeline();
  wireControls();
  wireCompare();
  updateDetail(it);
}

function updateDetail(it) {
  const p = it.probe || {};
  const facts = [];
  if (p.display_w) facts.push(`${p.display_w}×${p.display_h}`);
  if (p.fps_avg) facts.push(`${Math.round(p.fps_avg)} fps`);
  if (p.vcodec) facts.push(p.vcodec.toUpperCase());
  if (p.hdr) facts.push(`HDR ${p.hdr.toUpperCase()}`);
  $("#d-facts").textContent = facts.join(", ");

  const [cls, msg] = statusSentence(it);
  const sl = $("#d-status");
  sl.className = `status-line ${cls}`;
  sl.textContent = msg;

  $("#rot-text").textContent = rotationText(it);
  $("#trim-text").textContent = trimText(it);
  const ew = it.encoded_with;
  $("#src-rot-note").textContent = it.effective.rotation ? "(preview rotated)" : "";
  const q = qualityText(it);
  $("#out-note").textContent = it.has_output ? [ew && ew.start > 0 ? `starts at ${secs(ew.start)}` : "", q].filter(Boolean).join(", ") : "";

  // Buttons
  const busy = S.data.jobs.current?.id === it.id || S.data.jobs.queued.some((j) => j.id === it.id);
  const canApprove = it.status === "verified" && !it.stale && it.decision !== "approved";
  const finalized = it.status === "finalized";
  $("#b-approve").disabled = !canApprove || busy;
  $("#b-reject").disabled = finalized || it.decision === "rejected" || busy || !it.probe;
  const act = encodeAction(it);
  const eb = $("#b-encode");
  eb.hidden = !!act.hidden;
  eb.disabled = !!act.disabled;
  eb.textContent = act.label;
  eb.title = act.tip || "";
  eb.classList.toggle("btn-quiet", !!act.quiet);
  $("#b-trash").disabled = finalized || it.decision === "trash" || !it.source_exists;
  const undo = $("#b-undo");
  undo.hidden = finalized || !it.decision;
  undo.textContent = { approved: "Undo approve", rejected: "Undo reject", trash: "Keep video" }[it.decision] || "";
  $("#d-why").textContent = finalized ? "" : act.why || (!canApprove && it.needs_encode ? "Encode first to approve." : "");
  document.querySelectorAll("[data-rot],[data-trim]").forEach((b) => (b.disabled = finalized || !it.probe || !it.source_exists));

  drawTimeline(it);
  fitVideos(it);
}

// ------------------------------------------------------------------ players
function srcVid() { return $("#v-src"); }
function outVid() { return $("#v-out"); }

function attachWithFallback(video, url, proxyUrl, noteEl) {
  let triedProxy = false;
  video.src = url;
  video.addEventListener("error", () => {
    if (triedProxy) {
      if (noteEl) noteEl.textContent = "(can't play in this browser)";
      return;
    }
    triedProxy = true;
    if (noteEl) noteEl.textContent = "(making a preview copy…)";
    const t = video.currentTime;
    video.src = proxyUrl;
    video.addEventListener("loadedmetadata", () => {
      if (noteEl) noteEl.textContent = "(preview copy: judge quality with Frame check)";
      video.currentTime = t;
      fitVideos(item(S.selected));
    }, { once: true });
  });
}

function wirePlayers(it) {
  const id = encodeURIComponent(it.id);
  const sv = srcVid();
  if (it.source_exists) {
    attachWithFallback(sv, `/media/${id}/source`, `/proxy/${id}/source`, $("#src-play-note"));
  } else {
    $("#box-src").insertAdjacentHTML("beforeend", `<div class="veil">The original is no longer in this folder.</div>`);
  }
  const ov = outVid();
  if (ov) attachWithFallback(ov, `/media/${id}/output?rev=${it.rev}`, `/proxy/${id}/output?rev=${it.rev}`, $("#out-play-note"));

  sv.addEventListener("loadedmetadata", () => fitVideos(item(S.selected)));
  ov && ov.addEventListener("loadedmetadata", () => fitVideos(item(S.selected)));
  sv.addEventListener("timeupdate", () => syncOut(false));
  sv.addEventListener("seeked", () => syncOut(true));
  sv.addEventListener("play", () => { S.playing = true; $("#play").textContent = "Pause"; syncOut(true); loop(); });
  sv.addEventListener("pause", () => { S.playing = false; $("#play").textContent = "Play"; ov && ov.pause(); });
  sv.addEventListener("ended", () => { S.playing = false; $("#play").textContent = "Play"; });
  $("#play").addEventListener("click", togglePlay);
}

function togglePlay() {
  const sv = srcVid();
  if (!sv || !sv.src) return;
  sv.paused ? sv.play().catch(() => {}) : sv.pause();
}

function loop() {
  if (!S.playing) return;
  syncOut(false);
  drawPlayhead();
  requestAnimationFrame(loop);
}

function syncOut(force) {
  const it = item(S.selected);
  const sv = srcVid();
  const ov = outVid();
  drawPlayhead();
  if (!ov || !it || !it.encoded_with) return;
  const ew = it.encoded_with;
  const t = sv.currentTime;
  const veil = $("#out-veil");
  const inside = t >= ew.start - 0.02 && t <= ew.end + 0.02;
  if (!inside) {
    veil.hidden = false;
    veil.textContent = "This part is cut from the new file.";
    if (!ov.paused) ov.pause();
    return;
  }
  veil.hidden = true;
  const target = Math.max(0, t - ew.start);
  if (force || Math.abs(ov.currentTime - target) > 0.12) ov.currentTime = target;
  if (!sv.paused && ov.paused) ov.play().catch(() => {});
  if (sv.paused && !ov.paused) ov.pause();
}

function fitVideos(it) {
  if (!it) return;
  const place = (box, v, rot) => {
    if (!box || !v) return;
    const bw = box.clientWidth, bh = box.clientHeight;
    const sideways = rot === 90 || rot === 270;
    v.style.width = `${sideways ? bh : bw}px`;
    v.style.height = `${sideways ? bw : bh}px`;
    v.style.transform = `translate(-50%, -50%) rotate(${rot}deg)`;
  };
  place($("#box-src"), srcVid(), it.effective.rotation);
  place($("#box-out"), outVid(), 0);
}

// ------------------------------------------------------------------ timeline
function duration() { return item(S.selected)?.probe?.duration || 0; }
function xToT(clientX) {
  const r = $("#tl-track").getBoundingClientRect();
  return Math.min(duration(), Math.max(0, ((clientX - r.left) / r.width) * duration()));
}
const pos = (t) => `${(100 * t) / (duration() || 1)}%`;

function drawTimeline(it) {
  const d = duration();
  const track = $("#tl-track");
  if (!track) return;
  const start = S.dragging?.which === "start" ? S.dragging.t : it.effective.start;
  const end = S.dragging?.which === "end" ? S.dragging.t : it.effective.end;
  const speech = (it.trim?.speech || []).map(([a, b]) => `<div class="tl-speech" style="left:${pos(a)};width:${pos(b - a)}"></div>`).join("");
  track.innerHTML = speech
    + `<div class="tl-cut" style="left:0;width:${pos(start)}"></div>`
    + `<div class="tl-cut" style="left:${pos(end)};right:0"></div>`;
  $("#h-start").style.left = pos(start);
  $("#h-end").style.left = pos(end);
  $("#h-start").setAttribute("aria-valuetext", secs(start));
  $("#h-end").setAttribute("aria-valuetext", secs(end));
  const step = d > 600 ? 60 : d > 120 ? 15 : d > 30 ? 5 : d > 8 ? 1 : 0.5;
  let ticks = "";
  for (let t = 0; t <= d + 1e-6; t += step) ticks += `<span style="left:${pos(t)}">${secs(t).replace(/\.0$/, "")}</span>`;
  $("#tl-ticks").innerHTML = ticks;
  drawPlayhead();
}

function drawPlayhead() {
  const sv = srcVid();
  const head = $("#tl-head");
  if (!sv || !head) return;
  head.style.left = pos(sv.currentTime || 0);
  const it = item(S.selected);
  const ew = it?.encoded_with;
  const inOut = ew && sv.currentTime >= ew.start && sv.currentTime <= ew.end ? `, new file ${secs(sv.currentTime - ew.start)}` : "";
  $("#clock").innerHTML = `<b>${secs(sv.currentTime)}</b> / ${secs(duration())}${inOut}`;
}

function wireTimeline() {
  const tl = $("#tl");
  tl.addEventListener("pointerdown", (e) => {
    const h = e.target.closest(".tl-handle");
    if (h) {
      S.dragging = { which: h.id === "h-start" ? "start" : "end", t: xToT(e.clientX) };
      h.setPointerCapture(e.pointerId);
      e.preventDefault();
      return;
    }
    srcVid().currentTime = xToT(e.clientX);
  });
  tl.addEventListener("pointermove", (e) => {
    if (!S.dragging) return;
    S.dragging.t = xToT(e.clientX);
    srcVid().currentTime = S.dragging.t;
    drawTimeline(item(S.selected));
  });
  const finish = () => {
    if (!S.dragging) return;
    const it = item(S.selected);
    const { which, t } = S.dragging;
    S.dragging = null;
    const s = which === "start" ? t : it.effective.start;
    const en = which === "end" ? t : it.effective.end;
    setTrim({ start: Math.min(s, en), end: Math.max(s, en) });
  };
  tl.addEventListener("pointerup", finish);
  tl.addEventListener("pointercancel", finish);

  for (const id of ["h-start", "h-end"]) {
    $(`#${id}`).addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      e.stopPropagation();
      const it = item(S.selected);
      const d = (e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 1 : 0.1);
      const s = it.effective.start + (id === "h-start" ? d : 0);
      const en = it.effective.end + (id === "h-end" ? d : 0);
      setTrim({ start: s, end: en });
      srcVid().currentTime = id === "h-start" ? s : en;
    });
  }
}

// ------------------------------------------------------------------ edits
async function mutate(path, body, okMsg) {
  try {
    const it = await api(path, body);
    replaceItem(it);
    if (okMsg) toast(okMsg);
  } catch (e) {
    toast(e.message, true);
    refresh();
  }
}

function replaceItem(it) {
  const i = S.data.items.findIndex((x) => x.id === it.id);
  if (i >= 0) S.data.items[i] = it;
  renderList();
  if (it.id === S.selected) updateDetail(it);
}

function setTrim(body) { return mutate(`/api/items/${encodeURIComponent(S.selected)}/trim`, body); }

function wireControls() {
  document.querySelectorAll("[data-rot]").forEach((b) => b.addEventListener("click", () => {
    const it = item(S.selected);
    const v = b.dataset.rot;
    const rotation = v === "reset" ? null : (((it.effective.rotation + Number(v)) % 360) + 360) % 360;
    mutate(`/api/items/${encodeURIComponent(it.id)}/rotation`, { rotation });
  }));
  document.querySelectorAll("[data-trim]").forEach((b) => b.addEventListener("click", () => {
    const it = item(S.selected);
    const t = srcVid().currentTime;
    const v = b.dataset.trim;
    if (v === "reset") return setTrim({ reset: true });
    if (v === "none") return setTrim({ none: true });
    if (v === "start") return setTrim({ start: t, end: it.effective.end });
    return setTrim({ start: it.effective.start, end: t });
  }));
  $("#b-approve").addEventListener("click", () => decide("approved"));
  $("#b-reject").addEventListener("click", () => decide("rejected"));
  $("#b-trash").addEventListener("click", () => decide("trash"));
  $("#b-undo").addEventListener("click", () => decide(null));
  $("#b-encode").addEventListener("click", encodeOne);
}

async function decide(decision) {
  const it = item(S.selected);
  if (!it) return;
  if (decision === "approved" && $("#b-approve").disabled) return;
  if (decision === "rejected" && $("#b-reject").disabled) return;
  if (decision === "trash" && $("#b-trash").disabled) return;
  if (decision === null && $("#b-undo").hidden) return;
  const msg = { approved: "Approved", rejected: "Rejected. Original kept.", trash: "Marked for the Trash. Press U to undo." }[decision] || "Decision cleared";
  const idx = visibleItems().findIndex((x) => x.id === it.id);
  await mutate(`/api/items/${encodeURIComponent(it.id)}/decision`, { decision }, msg);
  if (decision === null) return;
  const after = visibleItems();
  if (after.some((x) => x.id === it.id)) {
    selectNext(1);
  } else if (after.length) {
    // The video just left this tab: show the one that took its place.
    select(after[Math.min(Math.max(idx, 0), after.length - 1)].id);
    scrollToSelected();
  }
}

// What the encode button should say and do for a video, in every state it can be in.
function encodeAction(it) {
  const cur = S.data.jobs.current;
  if (cur && cur.id === it.id) return { label: cur.kind === "verify" ? "Checking…" : "Encoding…", disabled: true };
  const q = S.data.jobs.queued.find((j) => j.id === it.id);
  if (q) return { label: "Queued", disabled: true, why: q.kind === "verify" ? "Waiting to be checked." : "Waiting its turn to encode." };
  if (it.status === "finalized") return { label: "Encode", hidden: true };
  if (!it.probe) return { label: "Encode", disabled: true, why: "Run pxlsqueeze analyze on this folder first." };
  if (!it.source_exists) return { label: "Encode", disabled: true, why: "The original is no longer in this folder." };
  if (it.status === "error") return { label: "Try again", tip: "Encode this video again" };
  if (it.decision === "rejected" || it.decision === "trash") return { label: "Encode", tip: "Clears your decision and encodes this video" };
  if (it.needs_encode) return it.encoded_with
    ? { label: "Encode with changes", tip: "Make a new file with your rotation and trim changes" }
    : { label: "Encode", tip: "Make the new file with the settings shown" };
  if (it.status === "encoded") return { label: "Check again", verify: true, tip: "Run the quality check on the new file again" };
  if (it.status === "skipped") return { label: "Encode anyway", body: { force: true, keep_small: true },
    tip: "Make and keep a new file even though it saves little space" };
  return { label: "Re-encode", body: { force: true }, quiet: true, tip: "Make the new file again with the same settings" };
}

async function encodeOne() {
  const it = item(S.selected);
  if (!it) return;
  const act = encodeAction(it);
  if (act.disabled || act.hidden) return;
  const id = encodeURIComponent(it.id);
  try {
    if (act.verify) {
      await api(`/api/items/${id}/verify`, {});
      toast("Queued for checking");
    } else {
      await api(`/api/items/${id}/encode`, act.body || {});
      toast("Queued for encoding");
    }
    refresh();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ frame compare
function wireCompare() {
  const go = $("#cmp-go");
  if (!go) return;
  go.addEventListener("click", () => {
    const it = item(S.selected);
    const sv = srcVid();
    sv.pause();
    const ew = it.encoded_with;
    let t = sv.currentTime;
    if (t < ew.start || t > ew.end) {
      t = (ew.start + ew.end) / 2;
      sv.currentTime = t;
      toast("That moment is cut from the new file, so the middle of the kept part is shown.");
    }
    S.compareT = t;
    const id = encodeURIComponent(it.id);
    $("#cmp-area").hidden = false;
    $("#cmp-actual").hidden = false;
    $("#cmp-info").textContent = `Loading full-resolution frames at ${secs(t)}…`;
    let loaded = 0;
    const done = () => { if (++loaded === 2) { $("#cmp-info").textContent = `Frame at ${secs(t)}. Drag the slider, or turn on Actual size to inspect detail.`; applyWipe(); } };
    const fail = () => { $("#cmp-info").textContent = "Couldn't extract that frame. Try a slightly different moment."; };
    const so = $("#cmp-src"), oo = $("#cmp-out");
    so.onload = done; oo.onload = done; so.onerror = fail; oo.onerror = fail;
    so.src = `/frame/${id}/source?t=${t.toFixed(3)}&rev=${it.rev}`;
    oo.src = `/frame/${id}/output?t=${t.toFixed(3)}&rev=${it.rev}`;
  });
  $("#wipe-range").addEventListener("input", applyWipe);
  $("#cmp-actual").addEventListener("click", (e) => {
    const on = e.currentTarget.getAttribute("aria-pressed") !== "true";
    e.currentTarget.setAttribute("aria-pressed", String(on));
    $("#wipe").classList.toggle("actual", on);
    applyWipe();
  });
  $("#wipe").addEventListener("pointermove", (e) => {
    if (e.buttons !== 1) return;
    const r = $("#cmp-out").getBoundingClientRect();
    $("#wipe-range").value = String(Math.round(100 * Math.min(1, Math.max(0, (e.clientX - r.left) / r.width))));
    applyWipe();
  });
}

function applyWipe() {
  const v = Number($("#wipe-range").value);
  const out = $("#cmp-out");
  const src = $("#cmp-src");
  src.style.width = `${out.offsetWidth}px`;
  src.style.height = `${out.offsetHeight}px`;
  src.style.clipPath = `inset(0 ${100 - v}% 0 0)`;
  $("#seam").style.left = `${(out.offsetWidth * v) / 100}px`;
}

// ------------------------------------------------------------------ selection + keys
function select(id) {
  if (S.selected === id) return;
  S.selected = id;
  S.renderedKey = null;
  S.playing = false;
  renderList();
  renderDetail();
}

function renderDetail() {
  const it = item(S.selected);
  if (!it) {
    const n = S.data.items.length;
    $("#detail").innerHTML = `<div class="empty">${n ? "Pick a video from the list." : "No PXL_*.mp4 videos were found in this folder."}</div>`;
    S.renderedKey = null;
    return;
  }
  const key = `${it.id}:${it.rev}:${it.has_output}:${it.source_exists}`;
  if (key !== S.renderedKey) {
    S.renderedKey = key;
    buildDetail(it);
  } else if (!S.dragging) {
    updateDetail(it);
  }
}

function scrollToSelected() {
  document.querySelector(`.item[data-id="${CSS.escape(S.selected)}"]`)?.scrollIntoView({ block: "nearest" });
}

function selectNext(dir) {
  const list = visibleItems();
  if (!list.length) return;
  const i = list.findIndex((x) => x.id === S.selected);
  const next = list[(i + dir + list.length) % list.length];
  if (next) select(next.id);
  scrollToSelected();
}

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, dialog") || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === " ") { e.preventDefault(); togglePlay(); }
  else if (k === "a") decide("approved");
  else if (k === "r") decide("rejected");
  else if (k === "t") decide("trash");
  else if (k === "u") decide(null);
  else if (k === "j") selectNext(1);
  else if (k === "k") selectNext(-1);
  else if (k === "e") encodeOne();
  else if (k === "[" || k === "]") {
    const it = item(S.selected);
    const sv = srcVid();
    if (!it || !sv || $("[data-trim]")?.disabled) return;
    setTrim(k === "[" ? { start: sv.currentTime, end: it.effective.end } : { start: it.effective.start, end: sv.currentTime });
  } else if (k === "arrowleft" || k === "arrowright") {
    const sv = srcVid();
    if (!sv) return;
    const fps = item(S.selected)?.probe?.fps_avg || 30;
    sv.pause();
    sv.currentTime = Math.max(0, sv.currentTime + (k === "arrowleft" ? -1 : 1) * (e.shiftKey ? 1 : 1 / fps));
  }
});

$("#list").addEventListener("click", (e) => {
  const b = e.target.closest(".item");
  if (b) select(b.dataset.id);
});
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  S.filter = b.dataset.filter;
  const list = visibleItems();
  if (list.length && !list.some((x) => x.id === S.selected)) select(list[0].id);
  renderList();
  scrollToSelected();
}));
window.addEventListener("resize", () => { fitVideos(item(S.selected)); if (!$("#cmp-area")?.hidden) applyWipe(); });

$("#trash-short").addEventListener("click", async () => {
  try {
    const r = await api("/api/trash-short", {});
    toast(`Marked ${r.marked.length} short clip${r.marked.length === 1 ? "" : "s"} for the Trash. Nothing is deleted until you finalize.`);
    refresh();
  } catch (e) { toast(e.message, true); }
});

$("#encode-all").addEventListener("click", async () => {
  try { await api("/api/encode-pending", {}); toast("Queued"); refresh(); } catch (e) { toast(e.message, true); }
});

$("#finalize").addEventListener("click", () => {
  const f = S.data.finalize;
  const dlg = $("#confirm");
  $("#confirm-title").textContent = `Move ${f.count} original${f.count === 1 ? "" : "s"} to the Trash?`;
  const parts = [];
  if (f.approved) parts.push(`${f.approved} approved video${f.approved === 1 ? "" : "s"} will be replaced by the new file. Each new file is checked again first, and any that fail keep their original.`);
  if (f.trash) parts.push(`${f.trash} video${f.trash === 1 ? "" : "s"} marked for the Trash will be removed with no copy kept.`);
  $("#confirm-body").textContent = `${parts.join(" ")} This frees about ${bytes(f.freed_bytes)}. You can restore anything from the Trash until you empty it.`;
  $("#confirm-ok").textContent = `Move ${f.count} to Trash`;
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", async () => {
    if (dlg.returnValue !== "ok") return;
    toast("Checking new files and moving originals…");
    try {
      const r = await api("/api/finalize", { count: f.count });
      const kept = r.failed.length ? ` ${r.failed.length} kept because their new file didn't pass the check.` : "";
      toast(`Moved ${r.done.length} originals to the Trash.${kept}`, r.failed.length > 0);
    } catch (e) { toast(e.message, true); }
    refresh();
  }, { once: true });
});

// ------------------------------------------------------------------ polling
async function refresh() {
  try {
    S.data = await api("/api/state");
  } catch (e) {
    $("#job").textContent = "Lost connection to pxlsqueeze. Is it still running?";
    return;
  }
  renderHeader();
  if (!S.selected) {
    if (!visibleItems().length && S.data.items.length) S.filter = "all";
    S.selected = visibleItems()[0]?.id ?? null;
  }
  renderList();
  renderDetail();
}

refresh();
setInterval(() => { if (!S.dragging) refresh(); }, 2000);
