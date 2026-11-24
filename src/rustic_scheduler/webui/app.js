"use strict";
// Everything shown comes from the scheduler's JSON routes and is inserted as text, never as HTML.

const POLL_MS = 3000;
const $ = (id) => document.getElementById(id);
let failures = 0;
let noticeTimer = null;
// Whether run output can be shown (web.run_logs), from the latest overview.
let runLogs = { available: false, message: null };
// What each operation does, with the official Rustic documentation for the command it runs.
const OPERATIONS = {
  backup: {
    text: "Saves the job's sources as a new snapshot.",
    docs: "https://rustic.cli.rs/docs/commands/backup/creating_snapshots.html",
  },
  snapshots: {
    text: "Lists the job's snapshots for the telemetry. Changes nothing.",
    docs: "https://rustic.cli.rs/docs/commands/misc/listing.html",
  },
  forget: {
    text: "Applies the job's retention rules to its own snapshots and removes the ones they do not keep. The scheduler previews the removal first.",
    docs: "https://rustic.cli.rs/docs/commands/forget/intro.html",
  },
  prune: {
    text: "Deletes repository data that no snapshot uses any more, and repacks partly used data.",
    docs: "https://rustic.cli.rs/docs/commands/forget/customize_pruning.html",
  },
  check: {
    text: "Verifies the repository's structure; with read-data it also reads the stored data.",
    docs: "https://rustic.cli.rs/docs/commands/misc/check.html",
  },
  init: {
    text: "Creates a new, empty repository at the job's location. If one is already there, nothing changes and the run succeeds.",
    docs: "https://rustic.cli.rs/docs/commands/init/intro.html",
  },
};
// Operations that ask before they start, and what the question says they do.
const CONFIRM = {
  forget: "removes backup data according to the job's configuration.",
  prune: "removes backup data according to the job's configuration.",
  init: "creates a new repository where the job's configuration points. Check that the location is the right one.",
};

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

function latestSnapshot(job) {
  const s = job.snapshots;
  if (!s) {
    return job.snapshots_enabled
      ? "Latest snapshot: not collected yet (snapshot telemetry has not succeeded since it was enabled)"
      : "Latest snapshot: unknown (snapshot telemetry is off for this job)";
  }
  if (!s.latest_at) return `Latest snapshot: none yet · checked ${when(s.collected_at)}`;
  // A total Rustic did not record is shown as unknown, never as 0.
  const size = s.latest_bytes === null ? "size unknown" : bytes(s.latest_bytes);
  const files = s.latest_files === null ? "file count unknown"
    : `${s.latest_files.toLocaleString()} ${s.latest_files === 1 ? "file" : "files"}`;
  return `Latest snapshot: ${when(s.latest_at)} · ${size} · ${files} · ` +
    `${s.count} ${s.count === 1 ? "snapshot" : "snapshots"} in total · checked ${when(s.collected_at)}`;
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
  if (CONFIRM[row.operation]) {
    const sure = window.confirm(
      `Run ${row.operation} for job "${job}" now?\n\n` +
      `${row.operation} ${CONFIRM[row.operation]}`
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
    if (response.ok) {
      notify(`${row.operation} for ${job} was queued. Queued is not a result: watch its state below.`, "ok");
      if (runLogs.available) {
        openConsole(job, row.operation, { previous: body.previous_run_id ?? null, requestedAt: body.requested_at });
      }
    } else {
      notify(`${row.operation} for ${job} was not started: ${body.message || response.statusText}`, "error");
    }
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
      const shown = row.state === "running" ? row.run_id : row.last_result && row.last_result.run_id;
      if (runLogs.available && shown) {
        const label = row.state === "running" ? "Output" : "Last output";
        const view = el("button", { type: "button", class: "plain", text: label,
          title: "Show Rustic's output of this run" });
        view.addEventListener("click", () => openConsole(job.name, row.operation, { run: shown }));
        action.append(view);
      }
      body.append(el("tr", null,
        el("td", { class: "name", text: row.operation }),
        describe(row.operation),
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
      job.description ? el("p", { class: "description", text: job.description }) : null,
      el("p", { class: "small snapshot", text: latestSnapshot(job) }),
      el("div", { class: "scroll" }, el("table", null,
        el("thead", null, el("tr", null,
          el("th", { text: "Operation" }), el("th", { text: "Description" }),
          el("th", { text: "State" }), el("th", { text: "Progress / next run" }),
          el("th", { text: "Last result" }), el("th", { text: "" }))),
        body))
    );
  });
  $("jobs").replaceChildren(...cards);
}

function describe(operation) {
  const info = OPERATIONS[operation];
  return el("td", { class: "small" }, info.text, " ",
    el("a", { href: info.docs, target: "_blank", rel: "noopener noreferrer", text: "Rustic docs" }));
}

function outputCell(item) {
  const cell = el("td", { class: "nowrap" });
  if (runLogs.available && item.run_id && item.log_file) {
    const view = el("button", { type: "button", class: "plain", text: "Output",
      title: "Show Rustic's output of this run" });
    view.addEventListener("click", () => openConsole(item.job, item.operation, { run: item.run_id }));
    cell.append(view);
  }
  return cell;
}

function renderHistory(data) {
  const rows = data.history.map((item) => el("tr", null,
    el("td", { class: "nowrap", text: when(item.finished_at) }),
    el("td", { text: item.job }),
    el("td", { text: item.operation }),
    el("td", null, badge(item.outcome)),
    el("td", { text: item.trigger }),
    el("td", { class: "nowrap", text: span(item.duration_seconds) }),
    el("td", { class: "detail", text: [item.reason !== "none" ? item.reason : "", item.detail].filter(Boolean).join(": ") }),
    outputCell(item)
  ));
  if (!rows.length) rows.push(el("tr", null, el("td", { colSpan: 8, class: "small", text: "Nothing has run yet." })));
  $("history").tBodies[0].replaceChildren(...rows);
  $("history-note").textContent =
    `The last ${data.history_size} finished or skipped runs since the scheduler started at ${when(data.started_at)}, ` +
    "plus the last known result of each operation from before. This list is kept in memory only; " +
    "long-term history is in Prometheus and in the repository's snapshots.";
}

function renderNextRuns(data) {
  const scheduled = data.jobs.flatMap((job) => job.operations
    .filter((row) => row.next_run)
    .map((row) => ({ job: job.name, ...row })));
  scheduled.sort((a, b) => new Date(a.next_run) - new Date(b.next_run));
  const rows = scheduled.map((row) => el("tr", null,
    el("td", { class: "nowrap", text: when(row.next_run) }),
    el("td", { text: row.job }),
    el("td", { text: row.operation }),
    el("td", { text: row.schedule })
  ));
  if (!rows.length) rows.push(el("tr", null, el("td", { colSpan: 4, class: "small", text: "No operations are scheduled." })));
  $("next").tBodies[0].replaceChildren(...rows);
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
    const plain = `rustic ${m.profiles}`;
    const dockerExec = [
      "docker exec -ti <rustic-scheduler-container> bash",
      m.docker_exec + " snapshots " + m.scope,
      "# or instead of 'snapshots' use: ls SNAPSHOT_ID",
      "# or: check",
      "# or: restore --dry-run SNAPSHOT_ID /restore/target",
      "# or: restore SNAPSHOT_ID:/path/in/snapshot /restore/target",
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
      block("In the scheduler's container (with environment variables from your config; replace <rustic-scheduler-container> with the container name or ID)", dockerExec),
      block(`With plain Rustic ${data.rustic_version}, in the container or on any machine that has these profile files and the secrets they refer to (adjust the paths)`, direct));
  });
  $("manual").replaceChildren(el("div", { class: "card" }, ...sections));
}

// ------------------------------------------------------------------ tabs

const TABS = ["jobs", "snapshots", "history", "next", "manual"];
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
  if (!response.ok) {
    const error = new Error(body.message || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
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
    const open = el("button", { type: "button", class: "plain", text: "📂", title: "Browse the files in this snapshot" });
    open.setAttribute("aria-label", `Browse files in snapshot ${snapshot.id.slice(0, 8)}`);
    const start = snapshot.paths.length === 1 ? snapshot.paths[0] : "/";
    const row = el("tr", null,
      el("td", { class: "nowrap", text: when(snapshot.time) }),
      el("td", { class: "mono", text: snapshot.id.slice(0, 8), title: snapshot.id }),
      el("td", { text: snapshot.hostname || "–" }),
      el("td", { class: "wrap", text: snapshot.paths.join(", ") }),
      el("td", { class: "num", text: snapshot.files === null ? "–" : snapshot.files.toLocaleString() }),
      el("td", { class: "num", text: bytes(snapshot.bytes) || "–" }),
      el("td", { class: "num", text: bytes(snapshot.bytes_added) || "–" }),
      el("td", { class: "wrap", text: snapshot.tags.join(", ") }),
      el("td", null, open)
    );
    open.addEventListener("click", () => {
      for (const other of $("snapshots").tBodies[0].rows) other.classList.remove("selected");
      row.classList.add("selected");
      browse.snapshot = snapshot;
      loadFiles(start);
    });
    return row;
  });
  if (!rows.length) rows.push(el("tr", null, el("td", { colSpan: 9, class: "small", text: "This job has no snapshot yet." })));
  $("snapshots").tBodies[0].replaceChildren(...rows);
  const count = `${data.snapshots.length} snapshot${data.snapshots.length === 1 ? "" : "s"}, listed ${when(data.fetched_at)}.`;
  note("browse-note", data.stale
    ? `${count} The repository is in use, so this list could not be renewed and may be out of date.`
    : `${count} Use Browse to see a snapshot's files.`, data.stale);
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

// ------------------------------------------------------------- run output

// The run's log is read from a byte offset: whole lines come back, and only new ones
// are appended. Everything is inserted as text.
const CONSOLE_POLL_MS = 1000;
const CONSOLE_MAX_CHARS = 2000000;
const term = { generation: 0, timer: null, job: null, operation: null, run: null, wait: null, offset: 0, chars: 0 };

function consoleStatus(text, error) {
  note("console-status", text, error);
}

function openConsole(job, operation, options) {
  term.generation += 1;
  clearTimeout(term.timer);
  Object.assign(term, {
    job, operation, run: options.run || null, wait: options.run ? null : options, offset: 0, chars: 0,
  });
  $("console-title").textContent = `${operation} · ${job}`;
  $("console-state").replaceChildren();
  $("console-text").replaceChildren();
  $("console-follow").checked = true;
  consoleStatus(options.run ? "Loading the output…" : "Waiting for the run to start…");
  if (!$("console").open) $("console").showModal();
  pollConsole(term.generation);
}

function stopConsole() {
  term.generation += 1;
  clearTimeout(term.timer);
}

function appendOutput(text, kind) {
  if (!text) return;
  const pre = $("console-text");
  const follow = $("console-follow").checked;
  const lines = text.endsWith("\n") ? text.slice(0, -1).split("\n") : text.split("\n");
  let plain = [];
  const flush = () => {
    if (plain.length) pre.append(document.createTextNode(plain.join("\n") + "\n"));
    plain = [];
  };
  for (const line of lines) {
    const cls = kind || (line.startsWith("[rustic-scheduler] ") || line.startsWith("$ ") ? "note" : null);
    if (cls) { flush(); pre.append(el("span", { class: cls, text: line + "\n" })); }
    else plain.push(line);
  }
  flush();
  term.chars += text.length;
  // A very long log keeps only its end in the page.
  while (term.chars > CONSOLE_MAX_CHARS && pre.firstChild) {
    term.chars -= pre.firstChild.textContent.length;
    pre.firstChild.remove();
  }
  if (follow) pre.scrollTop = pre.scrollHeight;
}

function showRun(state, run) {
  const head = $("console-state");
  if (state === "running") head.replaceChildren(badge("running"));
  else if (run) head.replaceChildren(badge(run.outcome), run.dry_run ? el("span", { class: "small", text: " dry run" }) : "");
  if (!run) return "";
  if (state === "running") return `Running since ${when(run.started_at)} · started by ${run.trigger}`;
  const parts = [`Finished ${when(run.finished_at)} after ${span(run.duration_seconds)} · started by ${run.trigger}`];
  if (run.attempts > 1) parts.push(`${run.attempts} attempts`);
  if (run.outcome !== "succeeded" && run.detail) parts.push(run.detail);
  return parts.join(" · ");
}

async function pollConsole(generation) {
  if (generation !== term.generation) return;
  const again = (ms) => { term.timer = setTimeout(() => pollConsole(generation), ms); };
  const query = new URLSearchParams({ offset: String(term.offset) });
  if (term.run) query.set("run", term.run);
  let data;
  try {
    data = await ask(`api/v1/jobs/${encodeURIComponent(term.job)}/${encodeURIComponent(term.operation)}/output?${query}`);
  } catch (error) {
    if (generation !== term.generation) return;
    consoleStatus(`The output could not be loaded: ${error.message}`, true);
    if (error.status !== 404 && error.status !== 403) again(CONSOLE_POLL_MS * 3);
    return;
  }
  if (generation !== term.generation) return;
  if (!term.run) {
    // After "Run now": wait for a run other than the one that was the latest then.
    const id = data.run ? data.run.run_id : null;
    if (data.state === "running" || (data.state === "finished" && id && id !== term.wait.previous)) {
      term.run = id;
    } else {
      const skip = data.last_skip;
      if (data.state !== "queued" && skip && term.wait.requestedAt &&
          new Date(skip.finished_at) >= new Date(term.wait.requestedAt)) {
        $("console-state").replaceChildren(badge("skipped"));
        consoleStatus(`The run did not start: ${skip.detail}`, true);
        return;
      }
      consoleStatus(data.state === "queued"
        ? "Queued: waiting for the repository, or for this job's other operations, to be free."
        : "Waiting for the run to start…");
      again(CONSOLE_POLL_MS);
      return;
    }
  }
  const status = showRun(data.state, data.run);
  const log = data.log;
  if (!log.available) {
    consoleStatus([status, `No output to show: ${log.message}`].filter(Boolean).join(" · "));
  } else {
    if (log.skipped && term.offset === 0) {
      appendOutput(`… the first ${bytes(log.skipped)} of this log are not shown …\n`, "gap");
    }
    appendOutput(log.text);
    term.offset = log.next_offset;
    consoleStatus(status);
  }
  if (log.available && log.more) again(0);
  else if (data.state !== "finished") again(CONSOLE_POLL_MS);
}

$("console-close").addEventListener("click", () => $("console").close());
$("console").addEventListener("close", stopConsole);
$("console-copy").addEventListener("click", async () => {
  const copy = $("console-copy");
  try { await navigator.clipboard.writeText($("console-text").textContent); copy.textContent = "Copied"; }
  catch { copy.textContent = "Select and copy by hand"; }
  setTimeout(() => { copy.textContent = "Copy"; }, 2000);
});
$("console-text").addEventListener("scroll", () => {
  // Scrolling up stops following; scrolling back to the end resumes it.
  const pre = $("console-text");
  $("console-follow").checked = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
});

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
    runLogs = data.run_logs;
    renderJobs(data);
    renderHistory(data);
    renderNextRuns(data);
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
