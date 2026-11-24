"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.events = {};
    this.attributes = {};
    this.textContent = "";
    this.classList = {
      add: (name) => { this.className = name; },
      remove: () => { this.className = ""; },
    };
  }
  get rows() { return this.children; }
  get firstChild() { return this.children[0]; }
  append(...children) {
    for (const child of children) if (child instanceof Element) child.parent = this;
    this.children.push(...children);
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  showModal() { this.open = true; }
  close() { this.open = false; if (this.events.close) this.events.close(); }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() { this.focused = true; }
}

const elements = new Map();
function getElementById(id) {
  if (!elements.has(id)) {
    const element = new Element(id);
    if (["next", "snapshots", "entries"].includes(id)) element.tBodies = [new Element("tbody")];
    elements.set(id, element);
  }
  return elements.get(id);
}
const timers = [];
const context = vm.createContext({
  document: {
    getElementById,
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => Object.assign(new Element("#text"), { textContent: text }),
  },
  window: { addEventListener() {} },
  location: { hash: "#next" },
  history: { replaceState(_state, _title, hash) { context.location.hash = hash; } },
  fetch: () => new Promise(() => {}),
  setInterval() {},
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout() {},
  URLSearchParams,
});
const source = fs.readFileSync(path.join(__dirname, "../../src/rustic_scheduler/webui/app.js"), "utf8");
vm.runInContext(source, context);

assert.equal(getElementById("panel-next").hidden, false);
assert.equal(getElementById("tab-next").attributes["aria-selected"], "true");
let prevented = false;
getElementById("tab-next").events.keydown({
  key: "ArrowLeft",
  preventDefault() { prevented = true; },
});
assert.equal(prevented, true);
assert.equal(context.location.hash, "#history");
assert.equal(getElementById("tab-history").focused, true);
getElementById("tab-next").events.click();
assert.equal(context.location.hash, "#next");

context.data = {
  jobs: [
    { name: "<job>", operations: [
      { operation: "backup", state: "running", next_run: "2026-06-02T03:00:00+02:00", schedule: "daily" },
      { operation: "check", next_run: null, schedule: null },
    ] },
    { name: "other", operations: [
      { operation: "check", state: "queued", next_run: "2026-06-02T00:30:00+00:00", schedule: "weekly" },
    ] },
  ],
};
vm.runInContext("renderNextRuns(data)", context);
const body = getElementById("next").tBodies[0];
assert.equal(body.children.length, 2);
assert.deepEqual(body.children.map((row) => row.children[1].textContent), ["other", "<job>"]);
assert.deepEqual(body.children.map((row) => row.children[2].textContent), ["check", "backup"]);
assert.equal(body.children[1].children[3].textContent, "daily");
assert.equal(body.children[1].children[1].children.length, 0);

context.data.jobs = [];
vm.runInContext("renderNextRuns(data)", context);
assert.equal(body.children.length, 1);
assert.equal(body.children[0].children[0].colSpan, 4);
assert.equal(body.children[0].children[0].textContent, "No operations are scheduled.");

// The jobs table: a Description column after Operation, and a confirmation before init.
function find(node, tag) {
  if (node.tag === tag) return node;
  for (const child of node.children) {
    const hit = child && typeof child === "object" ? find(child, tag) : null;
    if (hit) return hit;
  }
  return null;
}
context.data = {
  now: "2026-06-03T10:00:00Z", shutting_down: false,
  jobs: [{ name: "docs", label: "docs", snapshots: null, repository_busy: false, operations: [
    { operation: "backup", state: "idle", can_run: true, last_result: null, next_run: null, progress: null },
    { operation: "init", state: "idle", can_run: true, last_result: null, next_run: null, progress: null },
  ] }],
};
vm.runInContext("renderJobs(data)", context);
const rows = find(getElementById("jobs"), "tbody").children;
assert.equal(rows.length, 2);
assert.equal(rows[1].children[0].textContent, "init");
const description = rows[1].children[1];
assert.equal(description.children[0], "Creates a new, empty repository at the job's location. Rustic refuses if one is already there.");
assert.equal(description.children[2].href, "https://rustic.cli.rs/docs/commands/init/intro.html");
assert.equal(description.children[2].textContent, "Rustic docs");
assert.equal(rows[0].children[1].children[2].href, "https://rustic.cli.rs/docs/commands/backup/creating_snapshots.html");

// The snapshots operation has no row on the Jobs tab; an icon under the table opens the
// Snapshots tab with that job chosen.
context.data = {
  now: "2026-06-03T10:00:00Z", shutting_down: false,
  jobs: [{ name: "docs", label: "docs", snapshots: null, repository_busy: false, operations: [
    { operation: "snapshots", state: "idle", can_run: false, last_result: null, next_run: null, progress: null },
    { operation: "backup", state: "idle", can_run: true, last_result: null, next_run: null, progress: null },
  ] }],
};
vm.runInContext("renderJobs(data)", context);
const card = getElementById("jobs").children[0];
assert.deepEqual(find(card, "tbody").children.map((row) => row.children[0].textContent), ["backup"]);
const browseLink = card.children.at(-1).children[0];
assert.equal(browseLink.tag, "a");
assert.equal(browseLink.title, "Browse snapshots");
assert.equal(browseLink.attributes["aria-label"], "Browse snapshots of docs");
let followed = false;
browseLink.events.click({ preventDefault() { followed = true; } });
assert.equal(followed, true, "the link does not navigate by itself");
assert.equal(vm.runInContext("browse.job", context), "docs");
assert.equal(getElementById("browse-job").value, "docs");
assert.equal(context.location.hash, "#snapshots");

const asked = [];
let fetched = false;
context.window.confirm = (text) => { asked.push(text); return false; };
context.fetch = async () => { fetched = true; return {}; };
vm.runInContext('run("docs", { operation: "init" }, { disabled: false })', context).then(() => {
  assert.equal(asked.length, 1);
  assert.match(asked[0], /^Run init for job "docs" now\?/);
  assert.match(asked[0], /init creates a new repository/);
  assert.equal(fetched, false, "a cancelled init is not requested");
});

context.data = {
  version: "test", rustic_version: "test", ready: true, shutting_down: false,
  jobs: [{ name: "new", operations: [
    { operation: "backup", next_run: "2026-06-03T00:00:00Z", schedule: "daily" },
  ] }],
};
context.fetch = async () => ({ ok: true, json: async () => context.data });
vm.runInContext("renderJobs = renderHistory = renderManual = renderBrowse = () => {}", context);
vm.runInContext("refresh()", context).then(async () => {
  assert.equal(body.children[0].children[1].textContent, "new");

  const snapshots = [
    { id: "a".repeat(64), time: "2026-01-01T00:00:00Z", hostname: "host", paths: ["/data"],
      files: 1, bytes: 42, bytes_added: 42, tags: [] },
    { id: "b".repeat(64), time: "2026-01-02T00:00:00Z", hostname: "host", paths: ["/a", "/b"],
      files: null, bytes: null, bytes_added: null, tags: [] },
  ];
  const requests = [];
  context.fetch = async (url) => {
    requests.push(url);
    const data = url.includes("/files?")
      ? { path: url.endsWith("%2Fdata") ? "/data" : "/", entries: [], total: 0, truncated: false }
      : { snapshots, fetched_at: "2026-01-03T00:00:00Z", stale: false };
    return { ok: true, json: async () => data };
  };
  vm.runInContext('browse.job = "docs"', context);
  await vm.runInContext("loadSnapshots(false)", context);
  const snapshotBody = getElementById("snapshots").tBodies[0];
  assert.equal(snapshotBody.children.length, 2);
  for (const [index, row] of snapshotBody.children.entries()) {
    assert.equal(row.children.length, 9);
    assert.equal(row.children[0].textContent, new Date(snapshots[index].time).toLocaleString());
    assert.equal(row.children[0].children.length, 0);
    const button = row.children[8].children[0];
    assert.equal(button.tag, "button");
    assert.equal(button.title, "Browse the files in this snapshot");
    assert.equal(button.attributes["aria-label"], `Browse files in snapshot ${snapshots[index].id.slice(0, 8)}`);
    button.events.click();
    assert.equal(row.className, "selected");
    assert.equal(requests.at(-1), `api/v1/jobs/docs/snapshots/${snapshots[index].id}/files?path=${index ? "%2F" : "%2Fdata"}`);
  }
  assert.equal(snapshotBody.children[0].className, "");
  snapshots.length = 0;
  await vm.runInContext("loadSnapshots(false)", context);
  assert.equal(snapshotBody.children[0].children[0].colSpan, 9);

  // ------------------------------------------------ the run output window
  const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };
  const run = (extra) => ({ run_id: "NEW", trigger: "web", started_at: "2026-01-03T00:00:00Z", ...extra });
  const replies = [
    // Still the previous run: keep waiting.
    { state: "finished", run: run({ run_id: "OLD", outcome: "succeeded" }), last_skip: null,
      log: { available: true, offset: 0, next_offset: 5, text: "old\n", more: false, skipped: 0 } },
    { state: "running", run: run({ outcome: "unknown" }), last_skip: null,
      log: { available: true, offset: 0, next_offset: 25, text: "$ rustic backup\n<b>line</b>\n", more: true, skipped: 0 } },
    { state: "finished", run: run({ outcome: "failed", finished_at: "2026-01-03T00:00:03Z", duration_seconds: 3,
      attempts: 2, dry_run: false, detail: "exit status 1" }), last_skip: null,
      log: { available: true, offset: 25, next_offset: 60, text: "[rustic-scheduler] result: failed\n", more: false, skipped: 0 } },
  ];
  requests.length = 0;
  context.fetch = async (url) => {
    requests.push(url);
    const data = replies.shift();
    return { ok: true, json: async () => data };
  };
  vm.runInContext('openConsole("docs", "backup", { previous: "OLD", requestedAt: "2026-01-03T00:00:00Z" })', context);
  await settle();
  assert.equal(getElementById("console").open, true);
  assert.equal(getElementById("console-title").textContent, "backup · docs");
  assert.equal(getElementById("console-status").textContent, "Waiting for the run to start…");
  assert.equal(getElementById("console-text").children.length, 0);
  timers.shift().fn();
  await settle();
  const pre = getElementById("console-text");
  assert.deepEqual(pre.children.map((n) => [n.tag, n.className || "", n.textContent]), [
    ["span", "note", "$ rustic backup\n"],
    ["#text", "", "<b>line</b>\n"],
  ]);
  assert.equal(getElementById("console-state").children[0].textContent, "running");
  // More was already there: asked again at once, for the same run, from the new offset.
  assert.equal(timers[0].ms, 0);
  timers.shift().fn();
  await settle();
  assert.deepEqual(requests, [
    "api/v1/jobs/docs/backup/output?offset=0",
    "api/v1/jobs/docs/backup/output?offset=0",
    "api/v1/jobs/docs/backup/output?offset=25&run=NEW",
  ]);
  assert.equal(pre.children.at(-1).textContent, "[rustic-scheduler] result: failed\n");
  assert.equal(getElementById("console-state").children[0].textContent, "failed");
  assert.match(getElementById("console-status").textContent, /2 attempts · exit status 1$/);
  assert.equal(timers.length, 0, "a finished run is not asked for again");
  getElementById("console").close();
  assert.equal(getElementById("console").open, false);
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
