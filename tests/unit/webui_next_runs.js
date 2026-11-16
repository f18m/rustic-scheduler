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
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() { this.focused = true; }
}

const elements = new Map();
function getElementById(id) {
  if (!elements.has(id)) {
    const element = new Element(id);
    if (id === "next") element.tBodies = [new Element("tbody")];
    elements.set(id, element);
  }
  return elements.get(id);
}
const context = vm.createContext({
  document: { getElementById, createElement: (tag) => new Element(tag) },
  window: { addEventListener() {} },
  location: { hash: "#next" },
  history: { replaceState(_state, _title, hash) { context.location.hash = hash; } },
  fetch: () => new Promise(() => {}),
  setInterval() {},
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

context.data = {
  version: "test", rustic_version: "test", ready: true, shutting_down: false,
  jobs: [{ name: "new", operations: [
    { operation: "backup", next_run: "2026-06-03T00:00:00Z", schedule: "daily" },
  ] }],
};
context.fetch = async () => ({ ok: true, json: async () => context.data });
vm.runInContext("renderJobs = renderHistory = renderManual = renderBrowse = () => {}", context);
vm.runInContext("refresh()", context).then(() => {
  assert.equal(body.children[0].children[1].textContent, "new");
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
