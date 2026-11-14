"use strict";

/* keepcode window. Talks to Python through window.pywebview.api (see app/api.py). */

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const plural = (n, word) => `${n.toLocaleString("en")} ${word}${n === 1 ? "" : "s"}`;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const darkScheme = matchMedia("(prefers-color-scheme: dark)");
const ATTENTION = new Set(["critical", "warning"]);
const SEVERITY_WORD = { critical: "Not backed up", warning: "At risk", info: "Safe", ok: "Safe" };

let api = null;
const S = {
  info: null,
  rev: -1,
  projects: [],
  byId: new Map(),
  scan: {},
  backup: {},
  login: {},
  update: {},
  account: {},
  settings: null,
  selected: new Set(),
  tab: "attention",
  severity: null,
  query: "",
  offerDismissed: false,
  pendingBackup: null,
  listKey: "",
  seenResults: new Set(),
};

/* ------------------------------------------------------------------ field */

class Field {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.cell = 13;
    this.gap = 6;
    this.rows = 7;
    this.slots = new Map();
    this.marks = new Map(); // cell index -> { id, color, at }
    this.visited = 0;
    this.settled = false;
    this.scanning = false;
    this.sweepStart = 0;
    this.pulse = null;
    this.hover = -1;
    this.frame = 0;
    this.readPalette();
    this.resize();
    new ResizeObserver(() => {
      const before = this.cols;
      this.resize();
      if (this.cols !== before) this.relayout();
      this.kick();
    }).observe(canvas);
    canvas.addEventListener("mousemove", (e) => this.onMove(e));
    canvas.addEventListener("mouseleave", () => this.setHover(-1));
    canvas.addEventListener("click", () => this.onClick());
  }

  readPalette() {
    const css = getComputedStyle(document.documentElement);
    const v = (name) => css.getPropertyValue(name).trim();
    this.palette = {
      empty: v("--grid-empty"), visited: v("--grid-visited"), settled: v("--grid-settled"),
      sweep: v("--grid-sweep"), flash: v("--grid-flash"),
      critical: v("--red"), warning: v("--amber"), ok: v("--cyan"), info: v("--cyan"), line: v("--line-strong"),
    };
  }

  resize() {
    const ratio = window.devicePixelRatio || 1;
    const width = this.canvas.clientWidth;
    const height = this.canvas.clientHeight;
    this.canvas.width = Math.round(width * ratio);
    this.canvas.height = Math.round(height * ratio);
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.width = width;
    this.height = height;
    const step = this.cell + this.gap;
    this.cols = Math.max(12, Math.floor((width + this.gap) / step));
    this.size = this.cols * this.rows;
    this.offsetX = Math.round((width - (this.cols * step - this.gap)) / 2);
    this.offsetY = Math.round((height - (this.rows * step - this.gap)) / 2);
    let seed = 7;
    const random = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    this.order = Array.from({ length: this.size }, (_, i) => i);
    for (let i = this.order.length - 1; i > 0; i--) {
      const j = Math.floor(random() * (i + 1));
      [this.order[i], this.order[j]] = [this.order[j], this.order[i]];
    }
    this.rank = new Int32Array(this.size);
    this.order.forEach((cell, position) => { this.rank[cell] = position; });
  }

  slot(id) {
    if (this.slots.has(id)) return this.slots.get(id);
    let hash = 2166136261;
    for (const ch of id) hash = Math.imul(hash ^ ch.charCodeAt(0), 16777619) >>> 0;
    const taken = new Set(this.slots.values());
    let index = hash % this.size;
    for (let step = 0; step < this.size && taken.has(index); step++) index = (hash + step * 7) % this.size;
    this.slots.set(id, index);
    return index;
  }

  relayout() {
    const marks = [...this.marks.values()];
    this.slots.clear();
    this.marks.clear();
    for (const mark of marks) this.marks.set(this.slot(mark.id), { ...mark, at: 0 });
  }

  startScan() {
    this.scanning = true;
    this.settled = false;
    this.sweepStart = performance.now();
    this.marks.clear();
    this.slots.clear();
    this.visited = 0;
    this.kick();
  }

  setProjects(projects, reveal) {
    const now = performance.now();
    this.scanning = false;
    this.marks.clear();
    this.slots.clear();
    for (const p of projects) {
      const index = this.slot(p.id);
      const column = index % this.cols;
      const delay = reveal && !reducedMotion.matches ? 60 + (column / Math.max(this.cols - 1, 1)) * 700 : 0;
      this.marks.set(index, { id: p.id, color: p.severity, at: reveal ? now + delay : 0 });
    }
    this.visited = Math.max(this.visited, Math.round(this.size * 0.3));
    setTimeout(() => { this.settled = true; this.kick(); }, reveal ? 520 : 0);
    this.kick();
  }

  recolor(projects) {
    for (const p of projects) {
      const index = this.slot(p.id);
      const mark = this.marks.get(index);
      if (mark && mark.color !== p.severity) this.marks.set(index, { ...mark, color: p.severity, at: performance.now() });
      if (!mark) this.marks.set(index, { id: p.id, color: p.severity, at: 0 });
    }
    this.kick();
  }

  setPulse(id) {
    this.pulse = id ? this.slot(id) : null;
    this.kick();
  }

  cellAt(x, y) {
    const step = this.cell + this.gap;
    const col = Math.floor((x - this.offsetX + this.gap / 2) / step);
    const row = Math.floor((y - this.offsetY + this.gap / 2) / step);
    if (col < 0 || row < 0 || col >= this.cols || row >= this.rows) return -1;
    return row * this.cols + col;
  }

  onMove(event) {
    const rect = this.canvas.getBoundingClientRect();
    const index = this.cellAt(event.clientX - rect.left, event.clientY - rect.top);
    this.setHover(this.marks.has(index) ? index : -1);
  }

  setHover(index) {
    if (index === this.hover) return;
    this.hover = index;
    this.canvas.style.cursor = index >= 0 ? "pointer" : "default";
    const tip = $("#tooltip");
    const mark = this.marks.get(index);
    const project = mark && S.byId.get(mark.id);
    if (!project) {
      tip.hidden = true;
    } else {
      const step = this.cell + this.gap;
      tip.innerHTML = `<strong>${esc(project.name)}</strong><span class="tip-status" style="color:${this.palette[project.severity]}">${esc(SEVERITY_WORD[project.severity])}</span>`;
      tip.style.left = `${this.offsetX + (index % this.cols) * step + this.cell / 2}px`;
      tip.style.top = `${this.offsetY + Math.floor(index / this.cols) * step}px`;
      tip.hidden = false;
    }
    this.kick();
  }

  onClick() {
    const mark = this.marks.get(this.hover);
    if (mark) revealRow(mark.id);
  }

  sweepAt(now) {
    if (!this.scanning || reducedMotion.matches) return -99;
    const span = this.cols + 8;
    return (((now - this.sweepStart) / 1700) * span) % span - 4;
  }

  // Returns [color, glow]. Glow is only used for the cells under the sweep.
  colorOf(index, now, sweep) {
    const p = this.palette;
    const visited = this.rank[index] < this.visited;
    let color = visited ? p.settled : p.empty;
    let glow = false;
    if (this.scanning) {
      const behind = sweep - (index % this.cols);
      if (behind >= -0.5 && behind < 0.5) {
        color = p.sweep;
        glow = true;
      } else if (behind >= 0.5 && behind < 2.5) {
        color = p.visited;
      }
    }
    const mark = this.marks.get(index);
    if (mark && now >= mark.at) color = now - mark.at < 130 && mark.at > 0 ? p.flash : p[mark.color];
    if (index === this.pulse && Math.floor(now / 260) % 2 === 0) {
      color = p.sweep;
      glow = true;
    }
    return [color, glow];
  }

  draw(now) {
    const ctx = this.ctx;
    const step = this.cell + this.gap;
    const sweep = this.sweepAt(now);
    ctx.clearRect(0, 0, this.width, this.height);
    for (let index = 0; index < this.size; index++) {
      const x = this.offsetX + (index % this.cols) * step;
      const y = this.offsetY + Math.floor(index / this.cols) * step;
      const [color, glow] = this.colorOf(index, now, sweep);
      ctx.fillStyle = color;
      ctx.shadowColor = glow ? color : "transparent";
      ctx.shadowBlur = glow ? 12 : 0;
      ctx.beginPath();
      ctx.roundRect(x, y, this.cell, this.cell, 3);
      ctx.fill();
      ctx.shadowBlur = 0;
      if (index === this.hover) {
        ctx.strokeStyle = this.palette.flash;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.roundRect(x - 2.5, y - 2.5, this.cell + 5, this.cell + 5, 4.5);
        ctx.stroke();
      }
    }
  }

  animating(now) {
    if (this.scanning || this.pulse !== null) return true;
    for (const mark of this.marks.values()) if (mark.at && now - mark.at < 160) return true;
    return false;
  }

  kick() {
    if (this.frame) return;
    const tick = (now) => {
      this.frame = 0;
      if (this.scanning) this.visited = Math.min(Math.round(this.size * 0.3), Math.floor((S.scan.folders || 0) / 6 + (now - this.sweepStart) / 160));
      this.draw(now);
      if (this.animating(now)) this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }
}

let field = null;

/* -------------------------------------------------------------------- app */

async function start() {
  api = window.pywebview.api;
  field = new Field($("#field"));
  bindEvents();
  S.info = await api.init();
  api.smoke_ok();
  api.set_theme(darkScheme.matches);
  applySettings(S.info.settings);
  $("#about-version").textContent = S.info.version;
  if (S.info.justUpdated) toast(`keepcode was updated to ${S.info.version}.`);
  if (S.info.smoke) return;
  if (!S.info.git) {
    showGitMissing();
  } else {
    await scan();
  }
  poll();
}

async function scan() {
  S.listKey = "";
  S.seenResults.clear();
  if (await api.start_scan()) field.startScan();
  S.scan = { running: true, folders: 0 };
  render();
}

async function poll() {
  try {
    apply(await api.poll(S.rev));
  } catch (error) {
    console.error(error);
  }
  const busy = S.scan.running || S.backup.running || S.login.state === "waiting" || S.update.state === "downloading";
  setTimeout(poll, busy ? 100 : 900);
}

function apply(update) {
  const wasScanning = S.scan.running;
  Object.assign(S, { scan: update.scan, backup: update.backup, login: update.login, update: update.update, account: update.account });
  if (update.projects) {
    S.projects = update.projects;
    S.byId = new Map(S.projects.map((p) => [p.id, p]));
    S.rev = update.rev;
    if (wasScanning && !S.scan.running) {
      onScanDone();
    } else if (!S.scan.running) {
      field.recolor(S.projects);
    }
  } else if (wasScanning && !S.scan.running) {
    onScanDone();
  }
  field.setPulse(S.backup.running ? S.backup.current : null);
  for (const [id, result] of Object.entries(S.backup.results || {})) {
    if (!S.seenResults.has(id)) {
      S.seenResults.add(id);
      S.selected.delete(id);
    }
  }
  const current = S.backup.running ? S.backup.current : null;
  renderLogin();
  render();
  if (current && current !== S.followed) {
    S.followed = current;
    followRow(current);
  }
  if (!current) S.followed = null;
}

// Keeps the project being backed up in view, above the action bar.
function followRow(id) {
  const row = document.querySelector(`[data-row="${id}"]`);
  if (!row) return;
  const main = $("#main");
  const box = main.getBoundingClientRect();
  const bar = $("#actionbar");
  const top = box.top + 16;
  const bottom = (bar.hidden ? box.bottom : bar.getBoundingClientRect().top) - 16;
  const r = row.getBoundingClientRect();
  const behavior = reducedMotion.matches ? "auto" : "smooth";
  if (r.bottom > bottom) main.scrollBy({ top: r.bottom - bottom, behavior });
  else if (r.top < top) main.scrollBy({ top: r.top - top, behavior });
}

function onScanDone() {
  S.selected = new Set(S.projects.filter((p) => p.fixable && !p.copyOf).map((p) => p.id));
  S.tab = attentionCount() ? "attention" : "all";
  S.severity = null;
  field.setProjects(S.projects, true);
}

function attentionCount() {
  return S.projects.filter((p) => ATTENTION.has(p.severity)).length;
}

/* ----------------------------------------------------------------- render */

function render() {
  renderTop();
  renderHeadline();
  renderLegend();
  renderList();
  renderActionbar();
  renderOffer();
}

function renderTop() {
  const account = $("#account-button");
  if (S.account.state === "connected") {
    account.innerHTML = `<span class="avatar">${esc((S.account.login || "?")[0].toUpperCase())}</span><span>${esc(S.account.login)}</span>`;
    account.title = "Connected to GitHub";
  } else if (S.account.state === "disconnected") {
    account.innerHTML = `<svg><use href="#i-user"/></svg><span>Connect GitHub</span>`;
    account.title = "";
  } else {
    account.innerHTML = "";
  }
  account.hidden = !account.innerHTML;

  const pill = $("#update-pill");
  const u = S.update;
  if (u.state === "available") pill.textContent = `Update to ${u.version}`;
  else if (u.state === "downloading") pill.textContent = `Updating ${Math.round((u.progress || 0) * 100)}%`;
  else if (u.state === "restarting") pill.textContent = "Restarting";
  pill.hidden = !["available", "downloading", "restarting"].includes(u.state);
  renderUpdateDialog();
}

function renderHeadline() {
  const h1 = $("#headline");
  const sub = $("#sub");
  const scanButton = $("#scan-button");
  scanButton.disabled = !!(S.scan.running || S.backup.running);
  if (S.scan.error) {
    h1.textContent = "The scan stopped";
    sub.textContent = S.scan.error;
    return;
  }
  if (S.scan.running || !S.info) {
    h1.textContent = "Looking for projects";
    if (S.scan.phase === "check") sub.textContent = `Checking ${S.scan.checked || 0} of ${plural(S.scan.found || 0, "project")}`;
    else sub.innerHTML = `${plural(S.scan.folders || 0, "folder")} searched${S.scan.current ? `, now in <span class="path">${esc(S.scan.current)}</span>` : ""}`;
    return;
  }
  const b = S.backup;
  if (b.running) {
    const current = S.byId.get(b.current);
    h1.textContent = current ? `Backing up ${current.name}` : "Backing up";
    sub.textContent = `${b.index} of ${b.total}. ${b.message || ""}`;
    return;
  }
  const results = Object.values(b.results || {});
  if (b.finished && results.length) {
    const ok = results.filter((r) => r.ok).length;
    const failed = results.length - ok;
    h1.textContent = failed ? `${ok} backed up, ${failed} need${failed === 1 ? "s" : ""} your attention` : `${plural(ok, "project")} backed up`;
    sub.textContent = failed ? "The reason is next to each project below." : "Your code is safe on GitHub, in private repositories only you can see.";
    return;
  }
  const total = S.projects.length;
  const attention = attentionCount();
  if (!total) {
    h1.textContent = "No projects found";
    sub.textContent = `keepcode searched ${S.settings?.roots.join(", ") || S.info.home}. Add the folders where you keep code in Settings.`;
  } else if (!attention) {
    h1.textContent = "Everything is backed up";
    sub.textContent = `${plural(total, "project")} checked in ${plural(S.scan.folders || 0, "folder")}.`;
  } else {
    h1.textContent = `${plural(attention, "project")} need${attention === 1 ? "s" : ""} a backup`;
    sub.textContent = `Found ${plural(total, "project")} in ${plural(S.scan.folders || 0, "folder")}.`;
  }
}

function renderLegend() {
  const legend = $("#legend");
  if (S.scan.running || !S.projects.length) {
    legend.hidden = true;
    return;
  }
  const count = (sev) => S.projects.filter((p) => sev.includes(p.severity)).length;
  const items = [
    ["critical", ["critical"], "not backed up"],
    ["warning", ["warning"], "at risk"],
    ["ok", ["ok", "info"], "safe"],
  ];
  legend.innerHTML = items
    .map(([key, sev, label]) => `<button data-severity="${key}" aria-pressed="${S.severity === key}"><span class="sq ${key}"></span><b>${count(sev)}</b> ${label}</button>`)
    .join("");
  legend.hidden = false;
}

function visibleProjects() {
  const query = S.query.trim().toLowerCase();
  return S.projects.filter((p) => {
    if (S.severity) {
      const group = S.severity === "ok" ? ["ok", "info"] : [S.severity];
      if (!group.includes(p.severity)) return false;
    } else if (S.tab === "attention" && !ATTENTION.has(p.severity) && !(S.backup.results || {})[p.id]) {
      return false;
    }
    return !query || p.name.toLowerCase().includes(query) || p.path.toLowerCase().includes(query);
  });
}

function rowHtml(p) {
  const b = S.backup;
  const result = (b.results || {})[p.id];
  const working = b.running && b.current === p.id;
  const selectable = p.fixable && !b.running;
  const checked = S.selected.has(p.id);
  const select = p.fixable
    ? `<label class="check" title="${checked ? "Selected" : "Select"}"><input type="checkbox" data-select="${p.id}" ${checked ? "checked" : ""} ${selectable ? "" : "disabled"} aria-label="Back up ${esc(p.name)}"><span class="box"><svg><use href="#i-check"/></svg></span></label>`
    : `<span class="check"></span>`;
  const stacks = p.stacks.length ? `<span class="stack">${esc(p.stacks.slice(0, 3).join(", "))}</span>` : "";
  const copy = p.copyOf ? `<span class="badge" title="Looks like a copy of ${esc(p.copyOf)}">copy</span>` : "";

  let what;
  if (working) {
    what = `<div class="working"><span class="spinner"></span>${esc(b.message || "Starting")}</div>`;
  } else if (result) {
    const details = result.details.map((d) => `<div class="detail">${esc(d)}</div>`).join("");
    const again = result.blocked ? `<button class="secondary small inline-action" data-anyway="${p.id}">Back up anyway</button>` : "";
    what = `<div class="issue ${result.ok ? "result-ok" : "result-bad"}">${esc(result.summary)}</div>${details}${again}`;
  } else if (!p.issues.length) {
    what = `<div class="issue ok">Everything is pushed</div>`;
  } else {
    const first = p.issues.find((i) => i.kind !== "duplicate") || p.issues[0];
    let second = "";
    if (p.copyOf) second = `Looks like a copy of ${p.copyOf}`;
    else if (p.plan) second = p.createsRepo ? "Backs up to a new private GitHub repository" : `Saves your work to ${p.plan.replace(/^save to /, "")}`;
    else if (p.issues[1]) second = p.issues[1].message;
    what = `<div class="issue ${first.severity}">${esc(first.message)}</div>${second ? `<div class="plan">${esc(second)}</div>` : ""}`;
  }

  return `<div class="row${checked && p.fixable ? " is-selected" : ""}" role="listitem" data-row="${p.id}">
    <div class="select">${select}<span class="sq ${p.severity}" title="${SEVERITY_WORD[p.severity]}"></span></div>
    <div class="who"><div class="name"><strong>${esc(p.name)}</strong>${stacks}${copy}</div><div class="path" title="${esc(p.path)}">${esc(p.path)}</div></div>
    <div class="what">${what}</div>
    <div class="when">${esc(p.lastChange)}</div>
    <div class="row-actions">
      <button class="icon-button" data-open="${p.id}" title="Open folder" aria-label="Open the folder of ${esc(p.name)}"><svg><use href="#i-folder"/></svg></button>
      <button class="icon-button" data-skip="${p.id}" title="Skip this folder" aria-label="Skip ${esc(p.name)} in future scans"><svg><use href="#i-skip"/></svg></button>
    </div>
  </div>`;
}

function renderList() {
  const section = $("#projects");
  section.hidden = S.scan.running || !S.projects.length || !S.info?.git;
  if (section.hidden) return;
  $("#count-attention").textContent = attentionCount();
  $("#count-all").textContent = S.projects.length;
  for (const tab of $$(".tab")) tab.setAttribute("aria-selected", String(!S.severity && tab.dataset.filter === S.tab));
  const rows = visibleProjects();
  const key = JSON.stringify([S.rev, S.tab, S.severity, S.query, [...S.selected], S.backup.current, S.backup.message, S.backup.running, Object.keys(S.backup.results || {})]);
  if (key === S.listKey) return;
  S.listKey = key;
  $("#list").innerHTML = rows.map(rowHtml).join("");
  const empty = $("#empty-list");
  empty.hidden = rows.length > 0;
  if (!rows.length) empty.textContent = S.query ? `No project matches "${S.query}".` : "Nothing here. Every project in this group is safe.";
  const fixableVisible = rows.filter((p) => p.fixable);
  const allSelected = fixableVisible.length && fixableVisible.every((p) => S.selected.has(p.id));
  $("#select-all").textContent = allSelected ? "Select none" : "Select all";
  $("#select-all").disabled = !fixableVisible.length || S.backup.running;
}

function selectedProjects() {
  return S.projects.filter((p) => S.selected.has(p.id) && p.fixable);
}

function renderActionbar() {
  const bar = $("#actionbar");
  const b = S.backup;
  const chosen = selectedProjects();
  bar.hidden = !(b.running || (chosen.length && !S.scan.running));
  if (bar.hidden) return;
  const buttons = $(".actionbar-buttons");
  if (b.running) {
    $("#selection-count").textContent = `Backing up ${b.index} of ${b.total}`;
    $("#selection-plan").textContent = b.message || "";
    buttons.innerHTML = `<div class="progress"><div class="progress-bar" style="width:${Math.round(((b.index - 1) / b.total) * 100)}%"></div></div>`;
    return;
  }
  if (!$("#backup-button")) {
    buttons.innerHTML = `<button class="text" id="clear-selection">Clear</button><button class="primary" id="backup-button">Back up</button>`;
  }
  const repos = chosen.filter((p) => p.createsRepo).length;
  const saves = chosen.length - repos;
  const parts = [];
  if (repos) parts.push(repos === 1 ? "1 new private repository" : `${repos} new private repositories`);
  if (saves) parts.push(saves === 1 ? "1 save to a keepcode branch" : `${saves} saves to keepcode branches`);
  let plan = parts.join(" and ");
  if (repos && S.account.state !== "connected") plan += ". You will connect GitHub first.";
  $("#selection-count").textContent = `${plural(chosen.length, "project")} selected`;
  $("#selection-plan").textContent = plan;
  $("#backup-button").textContent = `Back up ${plural(chosen.length, "project")}`;
}

function renderOffer() {
  const offer = $("#daily-offer");
  const done = S.scan.done && !S.scan.running && !S.backup.running;
  const moment = (S.backup.finished && Object.keys(S.backup.results || {}).length) || (done && S.projects.length && !attentionCount());
  offer.hidden = !(S.settings && !S.settings.schedule.enabled && !S.offerDismissed && done && moment);
  $("#offer-time").textContent = S.settings?.scheduleTime || "18:00";
}

function revealRow(id) {
  const p = S.byId.get(id);
  if (!p) return;
  if (!visibleProjects().some((v) => v.id === id)) {
    S.tab = "all";
    S.severity = null;
    S.query = "";
    $("#search").value = "";
    render();
  }
  const row = document.querySelector(`[data-row="${id}"]`);
  if (!row) return;
  row.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "center" });
  row.classList.remove("is-flash");
  void row.offsetWidth;
  row.classList.add("is-flash");
}

/* ----------------------------------------------------------------- backup */

async function backup(ids, allowSecrets = false) {
  if (!ids.length) return;
  for (const id of ids) S.seenResults.delete(id);
  const answer = await api.start_backup(ids, allowSecrets);
  if (answer.started) {
    S.backup = { ...S.backup, running: true, total: ids.length, index: 1, results: S.backup.results || {} };
    render();
  } else if (answer.reason === "login") {
    S.pendingBackup = { ids, allowSecrets };
    openLogin();
  }
}

/* ------------------------------------------------------------------ login */

function openDialog(id) {
  $("#scrim").hidden = false;
  const dialog = $(id);
  dialog.hidden = false;
  dialog.querySelector("button:not([data-close])")?.focus();
}

function closeDialogs() {
  if (!$("#login-dialog").hidden && S.login.state === "waiting") api.login_cancel();
  for (const el of $$(".dialog, .drawer")) el.hidden = true;
  $("#scrim").hidden = true;
}

async function openLogin() {
  $("#login-error").hidden = true;
  $("#login-done").hidden = true;
  $("#login-device").hidden = true;
  $("#login-token").hidden = true;
  $("#login-intro").hidden = false;
  openDialog("#login-dialog");
  S.login = await api.login_start();
  renderLogin();
}

function renderLogin() {
  if ($("#login-dialog").hidden) return;
  const l = S.login;
  const error = $("#login-error");
  if (l.state === "waiting") {
    $("#login-device").hidden = false;
    const code = l.code || "";
    $("#login-code").innerHTML = [...code].map((c) => (c === "-" ? `<span class="dash">-</span>` : `<span>${esc(c)}</span>`)).join("");
    $("#copy-code span").textContent = l.copied ? "Copied" : "Copy";
  } else if (l.state === "token") {
    $("#login-token").hidden = false;
  } else if (l.state === "error") {
    error.textContent = l.error;
    error.hidden = false;
  } else if (l.state === "done" && $("#login-done").hidden) {
    $("#login-device").hidden = true;
    $("#login-token").hidden = true;
    $("#login-intro").hidden = true;
    error.hidden = true;
    $("#login-name").textContent = l.login;
    $("#login-done").hidden = false;
    setTimeout(() => {
      closeDialogs();
      const pending = S.pendingBackup;
      S.pendingBackup = null;
      if (pending) backup(pending.ids, pending.allowSecrets);
    }, 1100);
  }
}

/* ---------------------------------------------------------------- updates */

function renderUpdateDialog() {
  const u = S.update;
  if ($("#update-dialog").hidden) return;
  $("#update-version").textContent = u.version || "";
  const now = $("#update-now");
  const text = $("#update-text");
  $("#update-error").hidden = u.state !== "error";
  $("#update-error").textContent = u.error || "";
  $("#update-progress").hidden = !["downloading", "restarting"].includes(u.state);
  $("#update-bar").style.width = `${Math.round((u.state === "restarting" ? 1 : u.progress || 0) * 100)}%`;
  if (!u.canInstall) {
    text.innerHTML = `Update it with this command: <code>${esc(u.command || "")}</code>`;
    now.textContent = "Copy the command";
  } else if (u.state === "restarting") {
    text.textContent = "Restarting keepcode with the new version.";
    now.disabled = true;
  } else if (u.state === "downloading") {
    text.textContent = "Downloading and checking the new version.";
    now.disabled = true;
  } else {
    text.textContent = "The update downloads, checks that the file is genuine and restarts keepcode. It takes a few seconds.";
    now.textContent = "Update and restart";
    now.disabled = false;
  }
}

/* --------------------------------------------------------------- settings */

function applySettings(settings) {
  if (!settings) return;
  S.settings = settings;
  const roots = $("#roots");
  roots.innerHTML = settings.roots
    .map((r, i) => {
      const full = settings.rootsFull[i];
      const label = r === "~" ? `<span class="home">Your user folder</span> <span class="path">${esc(full)}</span>` : esc(r);
      return `<li><span title="${esc(full)}">${label}</span><button class="text tiny" data-unroot="${esc(full)}" ${settings.roots.length < 2 ? "disabled" : ""}>Remove</button></li>`;
    })
    .join("");
  const ignored = $("#ignored");
  ignored.innerHTML = settings.ignore.length
    ? settings.ignore.map((r, i) => `<li><span title="${esc(r)}">${esc(r)}</span><button class="text tiny" data-unskip="${esc(settings.ignoreFull[i])}">Stop skipping</button></li>`).join("")
    : `<li class="none">No folders are skipped. Use the skip button on a project to add one.</li>`;
  $("#schedule-toggle").checked = settings.schedule.enabled;
  $("#schedule-time").value = settings.scheduleTime;
  $("#schedule-detail").textContent = settings.schedule.enabled
    ? `Every day at ${settings.scheduleTime}, with a notification when something is at risk.`
    : "Off. keepcode only checks when you open it.";
  $("#autobackup-toggle").checked = settings.autoBackup;
  for (const button of $$("#update-mode button")) button.setAttribute("aria-pressed", String(button.dataset.mode === settings.autoUpdate));
  renderSettingsAccount();
  renderOffer();
}

function renderSettingsAccount() {
  const connected = S.account.state === "connected";
  $("#github-status").textContent = connected ? `Connected as ${S.account.login}.` : "Not connected. keepcode asks when it needs to create a repository.";
  $("#github-action").textContent = connected ? "Disconnect" : "Connect";
}

/* -------------------------------------------------------------- git state */

function showGitMissing() {
  $(".overview").hidden = true;
  $("#projects").hidden = true;
  $("#git-missing").hidden = false;
}

/* ------------------------------------------------------------------ toast */

let toastTimer = 0;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 4200);
}

/* ----------------------------------------------------------------- events */

function bindEvents() {
  $("#scan-button").addEventListener("click", scan);
  $("#settings-button").addEventListener("click", () => {
    renderSettingsAccount();
    openDialog("#settings");
  });
  $("#account-button").addEventListener("click", () => {
    if (S.account.state === "connected") {
      renderSettingsAccount();
      openDialog("#settings");
    } else openLogin();
  });
  $("#update-pill").addEventListener("click", () => {
    $("#update-dialog").hidden = false;
    $("#scrim").hidden = false;
    renderUpdateDialog();
  });
  $("#update-now").addEventListener("click", async () => {
    if (!S.update.canInstall) {
      await api.copy_text(S.update.command || "");
      toast("Command copied. Paste it in a terminal.");
      return;
    }
    await api.update_install();
  });
  $("#update-notes").addEventListener("click", () => api.open_url(S.update.url || "https://github.com/masterrr3470/keepcode/releases"));
  $("#scrim").addEventListener("click", closeDialogs);
  for (const button of $$("[data-close]")) button.addEventListener("click", closeDialogs);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDialogs();
    if (e.key === "/" && document.activeElement?.tagName !== "INPUT") {
      e.preventDefault();
      $("#search").focus();
    }
  });

  for (const tab of $$(".tab")) {
    tab.addEventListener("click", () => {
      S.tab = tab.dataset.filter;
      S.severity = null;
      render();
    });
  }
  $("#legend").addEventListener("click", (e) => {
    const button = e.target.closest("[data-severity]");
    if (!button) return;
    S.severity = S.severity === button.dataset.severity ? null : button.dataset.severity;
    render();
  });
  $("#search").addEventListener("input", (e) => {
    S.query = e.target.value;
    renderList();
  });
  $("#select-all").addEventListener("click", () => {
    const fixable = visibleProjects().filter((p) => p.fixable);
    const all = fixable.every((p) => S.selected.has(p.id));
    for (const p of fixable) all ? S.selected.delete(p.id) : S.selected.add(p.id);
    render();
  });

  $("#list").addEventListener("change", (e) => {
    const id = e.target.dataset.select;
    if (!id) return;
    e.target.checked ? S.selected.add(id) : S.selected.delete(id);
    render();
  });
  $("#list").addEventListener("click", async (e) => {
    const open = e.target.closest("[data-open]");
    const skip = e.target.closest("[data-skip]");
    const anyway = e.target.closest("[data-anyway]");
    if (open) api.open_folder(open.dataset.open);
    if (skip) {
      const p = S.byId.get(skip.dataset.skip);
      S.selected.delete(skip.dataset.skip);
      applySettings(await api.ignore_project(skip.dataset.skip));
      if (p) toast(`keepcode will skip ${p.path}. You can undo it in Settings.`);
    }
    if (anyway) backup([anyway.dataset.anyway], true);
  });

  $(".actionbar").addEventListener("click", (e) => {
    if (e.target.closest("#clear-selection")) {
      S.selected.clear();
      render();
    }
    if (e.target.closest("#backup-button")) backup(selectedProjects().map((p) => p.id));
  });

  $("#offer-dismiss").addEventListener("click", () => {
    S.offerDismissed = true;
    renderOffer();
  });
  $("#offer-accept").addEventListener("click", async () => {
    const settings = await api.schedule_set(true, S.settings.scheduleTime);
    applySettings(settings);
    toast(settings.error ? `Could not turn on the daily check: ${settings.error}` : `The daily check is on: every day at ${settings.scheduleTime}.`);
  });

  $("#copy-code").addEventListener("click", async () => {
    await api.copy_text(S.login.code || "");
    $("#copy-code span").textContent = "Copied";
  });
  $("#open-github").addEventListener("click", () => api.open_url(S.login.uri || "https://github.com/login/device"));
  $("#create-token").addEventListener("click", () => api.open_url("https://github.com/settings/tokens/new?scopes=repo&description=keepcode"));
  $("#save-token").addEventListener("click", async () => {
    S.login = await api.login_token($("#token-input").value);
    $("#token-input").value = "";
    renderLogin();
  });

  $("#add-root").addEventListener("click", async () => applySettings(await api.pick_root()));
  $("#roots").addEventListener("click", async (e) => {
    const button = e.target.closest("[data-unroot]");
    if (button) applySettings(await api.remove_root(button.dataset.unroot));
  });
  $("#ignored").addEventListener("click", async (e) => {
    const button = e.target.closest("[data-unskip]");
    if (button) applySettings(await api.unignore(button.dataset.unskip));
  });
  $("#schedule-toggle").addEventListener("change", async (e) => {
    const settings = await api.schedule_set(e.target.checked, $("#schedule-time").value);
    applySettings(settings);
    if (settings.error) toast(`Could not change the daily check: ${settings.error}`);
  });
  $("#schedule-time").addEventListener("change", async (e) => {
    if ($("#schedule-toggle").checked) applySettings(await api.schedule_set(true, e.target.value));
  });
  $("#autobackup-toggle").addEventListener("change", async (e) => applySettings(await api.settings_set({ autoBackup: e.target.checked })));
  $("#update-mode").addEventListener("click", async (e) => {
    const button = e.target.closest("[data-mode]");
    if (button) applySettings(await api.settings_set({ autoUpdate: button.dataset.mode }));
  });
  $("#github-action").addEventListener("click", async () => {
    if (S.account.state === "connected") {
      S.account = await api.logout();
      renderSettingsAccount();
      render();
    } else {
      closeDialogs();
      openLogin();
    }
  });
  for (const link of $$("[data-url]")) link.addEventListener("click", () => api.open_url(link.dataset.url));

  $("#install-git").addEventListener("click", () => api.install_git());
  $("#git-again").addEventListener("click", async () => {
    const answer = await api.git_check();
    if (answer.git) {
      $("#git-missing").hidden = true;
      $(".overview").hidden = false;
      S.info.git = true;
      scan();
    } else toast("Git is not installed yet. When the installer finishes, try again.");
  });

  darkScheme.addEventListener("change", (e) => {
    api.set_theme(e.matches);
    field.readPalette();
    field.kick();
  });
}

if (window.pywebview?.api) start();
else window.addEventListener("pywebviewready", start, { once: true });
