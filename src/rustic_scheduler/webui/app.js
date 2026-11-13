"use strict";
// Everything shown comes from the scheduler's JSON routes and is inserted as text, never as HTML.

const POLL_MS = 3000;
const $ = (id) => document.getElementById(id);
let failures = 0;
let noticeTimer = null;

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node[key] = value;
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function when(iso) {
  if (!iso) return "–";
  // Rustic prints nanoseconds; not every browser parses more than milliseconds.
  const date = new Date(iso.replace(/(\.\d{3})\d+/, "$1"));
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function span(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  let s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  s %= 60;
  return h ? `${h}h ${m}m` : `${m}m ${s}s`;
}

function bytes(n) {
  if (n === null || n === undefined) return null;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function badge(text) {
  return el("span", { class: `badge ${text}`, text });
}

function notify(text, kind) {
  const box = $("notice");
  box.textContent = text;
  box.className = kind;
  box.hidden = false;
  clearTimeout(noticeTimer);
  if (kind === "ok") noticeTimer = setTimeout(() => { box.hidden = true; }, 6000);
}

function activity(row, now) {
  if (row.state === "queued") return el("span", { class: "small", text: "waiting for its turn" });
  if (row.state !== "running") {
    return el("span", { class: "small", text: row.next_run ? `next: ${when(row.next_run)}` : "not scheduled" });
  }
  const box = el("div");
  const p = row.progress;
  const bar = el("progress");
  // No value: the browser draws an indeterminate bar. Unknown is not 0 %.
  if (p && p.ratio !== null) { bar.max = 1; bar.value = p.ratio; }
  box.append(bar);
  const parts = [];
  if (p && p.ratio !== null) parts.push(`${(p.ratio * 100).toFixed(1)} %`);
  if (p && p.bytes_done !== null) {
    parts.push(p.bytes_total !== null ? `${bytes(p.bytes_done)} of ${bytes(p.bytes_total)}` : bytes(p.bytes_done));
  }
  if (p && p.eta_seconds !== null) parts.push(`about ${span(p.eta_seconds)} left`);
  if (row.started_at) parts.push(`running for ${span((now - new Date(row.started_at)) / 1000)}`);
  if (!p) parts.push("Rustic has not reported progress");
  box.append(el("div", { class: "small", text: parts.join(" · ") }));
  return box;
}

function lastResult(row) {
  const last = row.last_result;
  const cell = el("td");
  if (!last) {
    cell.append(el("span", { class: "small", text: "no run recorded" }));
  } else {
    cell.append(badge(last.outcome), el("span", { class: "small", text: ` ${when(last.finished_at)} · ${span(last.duration_seconds)}` }));
    if (last.dry_run) cell.append(el("span", { class: "small", text: " · dry run" }));
    if (last.outcome !== "succeeded" && last.detail) cell.append(el("div", { class: "small", text: last.detail }));
  }
  const skip = row.last_skip;
  if (skip) cell.append(el("div", { class: "small", text: `skipped ${when(skip.finished_at)}: ${skip.detail}` }));
  return cell;
}

async function run(job, row, button) {
  if (row.destructive) {
    const sure = window.confirm(
      `Run ${row.operation} for job "${job}" now?\n\n` +
      `${row.operation} removes backup data according to the job's configuration.`
    );
    if (!sure) return;
  }
  button.disabled = true;
  try {
    const response = await fetch(
      `api/v1/jobs/${encodeURIComponent(job)}/${encodeURIComponent(row.operation)}/run`,
      { method: "POST", headers: { "X-Requested-With": "rustic-scheduler" } }
    );
    const body = await response.json().catch(() => ({}));
    if (response.ok) notify(`${row.operation} for ${job} was queued. Queued is not a result: watch its state below.`, "ok");
    else notify(`${row.operation} for ${job} was not started: ${body.message || response.statusText}`, "error");
  } catch (error) {
    notify(`${row.operation} for ${job} could not be requested: ${error.message}`, "error");
  }
  refresh();
}

function renderJobs(data) {
  const now = new Date(data.now);
  const cards = data.jobs.map((job) => {
    const body = el("tbody");
    for (const row of job.operations) {
      const action = el("td", { class: "nowrap" });
      if (row.can_run) {
        const button = el("button", { type: "button", text: "Run now", class: row.destructive ? "danger" : "" });
        button.disabled = row.state !== "idle" || data.shutting_down;
        button.addEventListener("click", () => run(job.name, row, button));
        action.append(button);
      }
      body.append(el("tr", null,
        el("td", { class: "name", text: row.operation }),
        el("td", null, badge(row.state)),
        el("td", null, activity(row, now)),
        lastResult(row),
        action
      ));
    }
    const facts = [`label ${job.label}`];
    if (job.host) facts.push(`host ${job.host}`);
    if (job.repository_busy) facts.push("repository in use");
    return el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h3", { text: job.name }), el("span", { class: "small", text: facts.join(" · ") })),
      el("div", { class: "scroll" }, el("table", null,
        el("thead", null, el("tr", null,
          el("th", { text: "Operation" }), el("th", { text: "State" }), el("th", { text: "Progress / next run" }),
          el("th", { text: "Last result" }), el("th", { text: "" }))),
        body))
    );
  });
  $("jobs").replaceChildren(...cards);
}

function renderHistory(data) {
  const rows = data.history.map((item) => el("tr", null,
    el("td", { class: "nowrap", text: when(item.finished_at) }),
    el("td", { text: item.job }),
    el("td", { text: item.operation }),
    el("td", null, badge(item.outcome)),
    el("td", { text: item.trigger }),
    el("td", { class: "nowrap", text: span(item.duration_seconds) }),
    el("td", { class: "detail", text: [item.reason !== "none" ? item.reason : "", item.detail].filter(Boolean).join(": ") })
  ));
  if (!rows.length) rows.push(el("tr", null, el("td", { colSpan: 7, class: "small", text: "Nothing has run yet." })));
  $("history").tBodies[0].replaceChildren(...rows);
  $("history-note").textContent =
    `The last ${data.history_size} finished or skipped runs since the scheduler started at ${when(data.started_at)}, ` +
    "plus the last known result of each operation from before. This list is kept in memory only; " +
    "long-term history is in Prometheus and in the repository's snapshots.";
}

function block(title, text) {
  const copy = el("button", { type: "button", class: "plain", text: "Copy" });
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(text); copy.textContent = "Copied"; }
    catch { copy.textContent = "Select and copy by hand"; }
    setTimeout(() => { copy.textContent = "Copy"; }, 2000);
  });
  return el("div", { class: "block" },
    el("div", { class: "block-head" }, el("span", { text: title }), copy),
    el("pre", { text }));
}

let manualKey = "";
function renderManual(data) {
  // Rebuilt only when it changes, so that open sections and selections survive the refresh.
  const key = JSON.stringify(data.jobs.map((job) => [job.name, job.manual])) + data.rustic_version;
  if (key === manualKey) return;
  manualKey = key;
  const sections = data.jobs.map((job, index) => {
    const m = job.manual;
    const inside = `docker compose exec rustic-scheduler ${m.helper}`;
    const plain = `rustic ${m.profiles}`;
    const container = [
      "# this job's snapshots",
      `${inside} snapshots ${m.scope}`,
      "# files in a snapshot (use an id from the list above)",
      `${inside} ls SNAPSHOT_ID`,
      "# check the repository's metadata",
      `${inside} check`,
      "# what a restore would write; restores no file (Rustic may create the empty target directory)",
      `${inside} restore --dry-run SNAPSHOT_ID /restore/target`,
    ].join("\n");
    const direct = [
      ...m.env.map((line) => `export ${line}`),
      `${plain} snapshots ${m.scope}`,
      `${plain} ls SNAPSHOT_ID`,
      `${plain} check`,
      `${plain} restore --dry-run SNAPSHOT_ID /restore/target`,
      `${plain} restore SNAPSHOT_ID:/path/in/snapshot /restore/target`,
    ].join("\n");
    return el("details", { open: index === 0 },
      el("summary", { text: job.name }),
      el("p", { class: "small", text:
        `Snapshots of this job carry the label "${m.label}"` + (m.host ? ` and the host "${m.host}".` : ".") }),
      block("In the scheduler's container (same profiles, environment and Rustic binary as the scheduler; adjust the service name to your Compose file)", container),
      block(`With plain Rustic ${data.rustic_version}, in the container or on any machine that has these profile files and the secrets they refer to (adjust the paths)`, direct));
  });
  $("manual").replaceChildren(el("div", { class: "card" }, ...sections));
}

// ------------------------------------------------------------------ tabs

const TABS = ["jobs", "snapshots", "history", "manual"];
let activeTab = "";

function showTab(name, focus) {
  if (!TABS.includes(name)) name = TABS[0];
  activeTab = name;
  for (const tab of TABS) {
    const button = $(`tab-${tab}`);
    const selected = tab === name;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    $(`panel-${tab}`).hidden = !selected;
    if (selected && focus) button.focus();
  }
  if (location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
  if (name === "snapshots") openSnapshots();
}

function setupTabs() {
  TABS.forEach((tab, index) => {
    const button = $(`tab-${tab}`);
    button.addEventListener("click", () => showTab(tab));
    button.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      showTab(TABS[(index + step + TABS.length) % TABS.length], true);
    });
  });
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  showTab(location.hash.slice(1));
}

// ------------------------------------------------- snapshots and their files

const SENT = { "X-Requested-With": "rustic-scheduler" };
const browse = { enabled: false, jobs: "", job: null, loaded: null, snapshot: null, request: 0 };

async function ask(url) {
  const response = await fetch(url, { cache: "no-store", headers: SENT });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message || `HTTP ${response.status}`);
  return body;
}

function note(id, text, error) {
  const box = $(id);
  box.textContent = text;
  box.className = error ? "small error" : "small";
}

function renderBrowse(data) {
  browse.enabled = data.browse;
  $("browse-off").hidden = data.browse;
  $("browse").hidden = !data.browse;
  const names = data.jobs.map((job) => job.name);
  if (JSON.stringify(names) !== browse.jobs) {
    browse.jobs = JSON.stringify(names);
    $("browse-job").replaceChildren(...names.map((name) => el("option", { value: name, text: name })));
    if (names.includes(browse.job)) $("browse-job").value = browse.job;
    else browse.job = names[0] || null;
  }
  if (activeTab === "snapshots") openSnapshots();
}

// Rustic is asked when the tab is opened, a job is chosen or "Refresh" is pressed,
// not on every poll.
function openSnapshots() {
  if (browse.enabled && browse.job && browse.loaded !== browse.job) loadSnapshots(false);
}

async function loadSnapshots(refresh) {
  const job = browse.job;
  const request = ++browse.request;
  browse.loaded = job;
  browse.snapshot = null;
  $("files").hidden = true;
  $("snapshots").tBodies[0].replaceChildren();
  note("browse-note", "Asking Rustic for the snapshots…");
  let data;
  try {
    data = await ask(`api/v1/jobs/${encodeURIComponent(job)}/snapshots${refresh ? "?refresh=1" : ""}`);
  } catch (error) {
    if (request === browse.request) note("browse-note", `The snapshots could not be listed: ${error.message}`, true);
    return;
  }
  if (request !== browse.request) return;
  const rows = data.snapshots.map((snapshot) => {
    const open = el("button", { type: "button", class: "link", text: when(snapshot.time), title: "List the files of this snapshot" });
    const start = snapshot.paths.length === 1 ? snapshot.paths[0] : "/";
    const row = el("tr", null,
      el("td", { class: "nowrap" }, open),
      el("td", { class: "mono", text: snapshot.id.slice(0, 8), title: snapshot.id }),
      el("td", { text: snapshot.hostname || "–" }),
      el("td", { class: "wrap", text: snapshot.paths.join(", ") }),
      el("td", { class: "num", text: snapshot.files === null ? "–" : snapshot.files.toLocaleString() }),
      el("td", { class: "num", text: bytes(snapshot.bytes) || "–" }),
      el("td", { class: "num", text: bytes(snapshot.bytes_added) || "–" }),
      el("td", { class: "wrap", text: snapshot.tags.join(", ") })
    );
    open.addEventListener("click", () => {
      for (const other of $("snapshots").tBodies[0].rows) other.classList.remove("selected");
      row.classList.add("selected");
      browse.snapshot = snapshot;
      loadFiles(start);
    });
    return row;
  });
  if (!rows.length) rows.push(el("tr", null, el("td", { colSpan: 8, class: "small", text: "This job has no snapshot yet." })));
  $("snapshots").tBodies[0].replaceChildren(...rows);
  const count = `${data.snapshots.length} snapshot${data.snapshots.length === 1 ? "" : "s"}, listed ${when(data.fetched_at)}.`;
  note("browse-note", data.stale
    ? `${count} The repository is in use, so this list could not be renewed and may be out of date.`
    : `${count} Choose one to see its files.`, data.stale);
}

function permissions(entry) {
  if (entry.permissions === null) return "–";
  const kind = { dir: "d", symlink: "l", file: "-" }[entry.type] || "?";
  let text = "";
  for (let bit = 8; bit >= 0; bit--) text += (entry.permissions >> bit) & 1 ? "rwx"[(8 - bit) % 3] : "-";
  return kind + text;
}

function owner(entry) {
  const user = entry.user || (entry.uid === null ? null : String(entry.uid));
  const group = entry.group || (entry.gid === null ? null : String(entry.gid));
  return user === null && group === null ? "–" : `${user ?? "?"}:${group ?? "?"}`;
}

function crumbs(path) {
  const parts = path.split("/").filter(Boolean);
  const nodes = [];
  const add = (label, target, last) => {
    if (last) { nodes.push(el("strong", { text: label })); return; }
    const button = el("button", { type: "button", class: "link", text: label });
    button.addEventListener("click", () => loadFiles(target));
    nodes.push(button);
  };
  add("/", "/", parts.length === 0);
  parts.forEach((part, index) => {
    if (index) nodes.push(el("span", { text: "/" }));
    add(part, "/" + parts.slice(0, index + 1).join("/"), index === parts.length - 1);
  });
  $("crumbs").replaceChildren(...nodes);
}

async function loadFiles(path) {
  const snapshot = browse.snapshot;
  const request = ++browse.request;
  $("files").hidden = false;
  $("files-title").textContent = `Files in the snapshot of ${when(snapshot.time)} (${snapshot.id.slice(0, 8)})`;
  note("files-note", "Asking Rustic for this directory…");
  let data;
  try {
    data = await ask(
      `api/v1/jobs/${encodeURIComponent(browse.job)}/snapshots/${snapshot.id}/files?path=${encodeURIComponent(path)}`);
  } catch (error) {
    if (request === browse.request) note("files-note", `${path} could not be listed: ${error.message}`, true);
    return;
  }
  if (request !== browse.request) return;
  crumbs(data.path);
  const rows = data.entries.map((entry) => {
    const name = el("td", { class: "wrap" });
    if (entry.type === "dir") {
      const open = el("button", { type: "button", class: "link", text: `${entry.name}/` });
      open.addEventListener("click", () => loadFiles(`${data.path === "/" ? "" : data.path}/${entry.name}`));
      name.append(open);
    } else {
      name.textContent = entry.link_target ? `${entry.name} → ${entry.link_target}` : entry.name;
    }
    return el("tr", null,
      name,
      el("td", { class: "num", text: bytes(entry.size) || "–" }),
      el("td", { class: "nowrap", text: when(entry.modified) }),
      el("td", { class: "mono", text: permissions(entry) }),
      el("td", { class: "nowrap", text: owner(entry) })
    );
  });
  if (!rows.length) rows.push(el("tr", null, el("td", { colSpan: 5, class: "small", text: "This directory is empty." })));
  $("entries").tBodies[0].replaceChildren(...rows);
  note("files-note", data.truncated
    ? `Showing the first ${data.entries.length.toLocaleString()} of ${data.total.toLocaleString()} entries (web.browse.max_entries).`
    : `${data.total.toLocaleString()} ${data.total === 1 ? "entry" : "entries"}.`);
}

$("browse-job").addEventListener("change", () => { browse.job = $("browse-job").value; loadSnapshots(false); });
$("browse-refresh").addEventListener("click", () => { if (browse.job) loadSnapshots(true); });

async function refresh() {
  try {
    const response = await fetch("api/v1/overview", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    failures = 0;
    if ($("notice").className === "error stale") $("notice").hidden = true;
    const state = data.shutting_down ? "shutting down" : data.ready ? "running" : "not ready";
    $("summary").textContent =
      `Scheduler ${data.version} · Rustic ${data.rustic_version} · ${state} · updated ${new Date().toLocaleTimeString()}`;
    renderJobs(data);
    renderHistory(data);
    renderManual(data);
    renderBrowse(data);
  } catch (error) {
    failures += 1;
    if (failures >= 2) notify(`Cannot reach the scheduler (${error.message}). What is shown may be out of date.`, "error stale");
  }
}

setupTabs();
refresh();
setInterval(refresh, POLL_MS);
