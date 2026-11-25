"use strict";

// Run with: node --test tests/editors_newlines.test.cjs
// No DOM dependency: the fixture models textarea value normalization and runs
// the complete shipped script, including its event handlers and fetch payloads.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const scriptPath = path.join(__dirname, "../src/helheim/static/editors.js");
const script = fs.readFileSync(scriptPath, "utf8");
const { normal, newlineDraft } = require(scriptPath);

const cases = [
  ["LF edit", "first\nsecond\n", "changed\nsecond\n", "changed\nsecond\n"],
  ["CRLF edit", "first\r\nsecond\r\n", "changed\nsecond\n", "changed\r\nsecond\r\n"],
  ["CR edit", "first\rsecond\r", "changed\nsecond\n", "changed\rsecond\r"],
  ["mixed edit", "α\r\nβ\nγ\rδ", "α\nB\nγ\nδ", "α\r\nB\nγ\rδ"],
  ["insert middle", "a\r\nb\nc\r", "a\nnew\nb\nc\n", "a\r\nnew\r\nb\nc\r"],
  ["insert start", "a\r\nb\n", "new\na\nb\n", "new\r\na\r\nb\n"],
  ["delete middle", "a\r\nb\nc\rd", "a\nc\nd", "a\r\nc\rd"],
  ["split line", "alpha\r\nbeta\n", "al\npha\nbeta\n", "al\r\npha\r\nbeta\n"],
  ["join lines", "a\r\nb\nc\r", "ab\nc\n", "ab\r\nc\r"],
  ["add trailing newline", "a\r\nb", "a\nb\n", "a\r\nb\r\n"],
  ["remove trailing newline", "a\r\nb\n", "a\nb", "a\r\nb"],
  ["empty original", "", "😀\n漢字\n", "😀\n漢字\n"],
  ["empty replacement", "a\r\nb\n", "", ""],
  ["blank lines", "a\r\n\r\nb\n", "a\n\nB\n", "a\r\n\r\nB\n"],
  ["repeated lines", "a\r\nsame\nsame\rb", "a\nsame\nb", "a\r\nsame\nb"],
  ["Unicode", "😀é\r\n漢字\rZ\n", "😀é\n漢字\nZ!\n", "😀é\r\n漢字\rZ!\n"],
  ["new CR/LF adjacency cannot fuse two breaks", "a\rb\n\nc", "a\n\nc", "a\r\r\nc"],
];
for (const [name, original, edited, expected] of cases) {
  test("mapper: " + name, () => {
    assert.equal(newlineDraft(original, edited), expected);
    assert.equal(normal(newlineDraft(original, edited)), normal(edited));
  });
}

function measuredMapper() {
  const stats = { maps: 0, copiedEntries: 0, writes: 0, reads: 0, binarySteps: 0 };
  class MeasuredMap extends Map {
    constructor(values) { super(values); stats.maps++; stats.copiedEntries += this.size; }
    set(key, value) { stats.writes++; return super.set(key, value); }
    get(key) { stats.reads++; return super.get(key); }
    has(key) { stats.reads++; return super.has(key); }
  }
  const measuredMath = Object.create(Math);
  measuredMath.floor = (value) => { stats.binarySteps++; return Math.floor(value); };
  const context = { module: { exports: {} }, Map: MeasuredMap, Math: measuredMath };
  vm.runInNewContext(script, context, { filename: scriptPath });
  return { map: context.module.exports.newlineDraft, stats };
}

for (const ending of ["\n", "\r\n", "\r"]) {
  test("large uniform wholesale replacement uses no alignment: " + JSON.stringify(ending), () => {
    const { map, stats } = measuredMapper();
    const original = Array.from({ length: 10000 }, (_, i) => "old" + i + ending).join("");
    const draft = Array.from({ length: 10000 }, (_, i) => "new😀" + i + "\n").join("");
    assert.equal(map(original, draft), draft.replace(/\n/g, ending));
    assert.equal(stats.maps, 0);
    assert.equal(stats.binarySteps, 0);
  });
}

test("10k mixed-line wholesale replacement keeps positional separators with linear index storage", () => {
  const { map, stats } = measuredMapper();
  const endings = ["\r\n", "\n", "\r"];
  const original = Array.from({ length: 10000 }, (_, i) => "old" + i + endings[i % 3]).join("");
  const draft = Array.from({ length: 10000 }, (_, i) => "new😀" + i + "\n").join("");
  const output = map(original, draft);
  assert.equal(normal(output), draft);
  assert.equal(output, original.replace(/old/g, "new😀"));
  // Deterministic operation/storage bounds, not a wall-clock ratio. In particular,
  // copying the old distance frontier per edit would fail this on tiny inputs too.
  assert.equal(stats.maps, 2);
  assert.equal(stats.copiedEntries, 0);
  assert.equal(stats.writes, 20002);
  assert.ok(stats.reads <= 5 * 20002);
  assert.ok(stats.binarySteps <= 10001 * Math.ceil(Math.log2(10002)));
});

test("10k mixed unique anchors with insertions retain all unrelated delimiters", () => {
  const { map, stats } = measuredMapper();
  const endings = ["\r\n", "\n", "\r"];
  const original = Array.from({ length: 10000 }, (_, i) => "line" + i + endings[i % 3]).join("");
  const draft = normal(original).replace("line5000\n", "inserted\nline5000\n");
  assert.equal(map(original, draft), original.replace("line5000", "inserted\nline5000"));
  assert.equal(stats.maps, 2);
  assert.equal(stats.copiedEntries, 0);
  assert.ok(stats.writes <= 20003);
  assert.ok(stats.binarySteps <= 10001 * Math.ceil(Math.log2(10002)));
});

test("10k reversed unique lines have bounded anchor work and exact normalized content", () => {
  const { map, stats } = measuredMapper();
  const lines = Array.from({ length: 10000 }, (_, i) => "line" + i);
  const original = lines.map((line, i) => line + (i % 2 ? "\n" : "\r\n")).join("");
  const draft = lines.reverse().join("\n");
  assert.equal(normal(map(original, draft)), draft);
  assert.equal(stats.maps, 2);
  assert.equal(stats.copiedEntries, 0);
  assert.ok(stats.writes <= 20001);
  assert.ok(stats.binarySteps <= 10000 * Math.ceil(Math.log2(10001)));
});

test("thousands of repeated mixed lines keep matched prefix/suffix delimiters", () => {
  const endings = ["\r\n", "\n", "\r"];
  const prefix = Array.from({ length: 3000 }, (_, i) => "same" + endings[i % 3]).join("");
  const suffix = Array.from({ length: 3000 }, (_, i) => "repeat" + endings[i % 3]).join("");
  const original = prefix + "anchor\r\nold\nend-anchor\r" + suffix;
  const expected = prefix + "anchor\r\nnew\ninserted\nend-anchor\r" + suffix;
  const draft = normal(expected);
  assert.equal(newlineDraft(original, draft), expected);
});

test("small mixed/repeated edits always reconstruct the exact normalized draft", () => {
  const texts = [""];
  for (const a of ["", "a", "b"]) {
    texts.push(a);
    for (const b of ["", "a", "b"]) {
      texts.push(a + "\n" + b);
      for (const c of ["", "a", "b"]) texts.push(a + "\n" + b + "\n" + c);
    }
  }
  for (const originalText of texts) {
    for (const pattern of [["\r", "\n"], ["\n", "\r\n"], ["\r\n", "\r"]]) {
      let index = 0;
      const original = originalText.replace(/\n/g, () => pattern[index++ % pattern.length]);
      for (const draft of texts) {
        assert.equal(normal(newlineDraft(original, draft)), draft, JSON.stringify({ original, draft }));
      }
    }
  }
});

class Element {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.listeners = {};
    this.children = new Map();
    this.groups = new Map();
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this._value = "";
  }
  set value(value) { this._value = normal(value); }
  get value() { return this._value; }
  querySelector(selector) {
    if (!this.children.has(selector)) this.children.set(selector, new Element());
    return this.children.get(selector);
  }
  querySelectorAll(selector) { return this.groups.get(selector) || []; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute() {}
  removeAttribute() {}
  append() {}
  async fire(name, event) { if (this.listeners[name]) await this.listeners[name](event); }
}

function formEvent() { return { preventDefault() {} }; }

function editor(kind, original) {
  const state = kind === "source"
    ? { kind, id: "workspace", original, head: "head-0", document_hash: "hash-0",
      procedure: "wrap-up", backend: "db", document_id: "db",
      document_kind: "backend", document_path: "procedures/memory/storage-backends/db.md",
      region: "wrap-up.report" }
    : { kind, id: "memory", body: original,
      context: { uuid: "uuid", tenant: "tenant", revision: "revision-0" } };
  const root = new Element({ editorInit: JSON.stringify(state) });
  const text = new Element();
  const prLink = new Element();
  prLink.hidden = true;
  const actions = root.querySelector("[data-workspace]");
  const actionQuery = actions.querySelector.bind(actions);
  actions.querySelector = (selector) => selector === "[data-pr-link]" ? null : actionQuery(selector);
  const metadata = new Element();
  metadata.value = '{"title":"before"}';
  const buttons = {};
  for (const area of kind === "source" ? ["source"] : ["body", "metadata"]) {
    for (const action of kind === "source" ? ["save"] : ["preview", "save"]) {
      const button = new Element(kind === "source"
        ? { sourceAction: action } : { memoryAction: action, area });
      buttons[area + "-" + action] = button;
      if (kind === "source") root.children.set(`[data-source-action="${action}"]`, button);
    }
  }
  const allButtons = Object.values(buttons);
  const close = new Element();
  if (kind !== "source") {
    root.children.set("[data-memory-close]", close);
    allButtons.push(close);
  }
  root.groups.set("button", allButtons);
  root.groups.set("[data-source-action]", allButtons.filter((b) => b.dataset.sourceAction));
  root.groups.set("[data-memory-action]", allButtons.filter((b) => b.dataset.memoryAction));
  root.groups.set('[data-memory-action="save"]', allButtons.filter((b) => b.dataset.memoryAction === "save"));
  const requests = [];
  const window = new Element();
  const navigations = [];
  const selectors = new Element();
  const currentView = { procedure: "wrap-up", backend: "db", document: "db", revision: "branch" };
  const documentPaths = { db: "procedures/memory/storage-backends/db.md", another: "procedures/another.md" };
  selectors.action = "/procedures/workspace";
  selectors.reset = () => { selectors.values = { ...currentView }; };
  selectors.reset();
  selectors.requestSubmit = () => selectors.fire("submit", formEvent());
  selectors.elements = { namedItem(name) {
    assert.equal(name, "document");
    return { selectedOptions: [{ dataset: { path: documentPaths[selectors.values.document] } }] };
  } };
  let confirm = true;
  let confirmCount = 0;
  window.confirm = () => { confirmCount++; return confirm; };
  window.location = { assign(url) { navigations.push(url); } };
  let revision = 0;
  let fail = false;
  let closeGate;
  let saveGate;
  let saveReply;
  let closeResult = { closed: true };
  const document = {
    querySelector(selector) {
      if (selector === 'meta[name="csrf-token"]') return { content: "token" };
      if (selector === "[data-editor-init]") return root;
      if (selector === "[data-pr-link]") return prLink;
      if (selector === "[data-new-workspace]") return null;
      if (selector === "[data-procedure-selectors]") return kind === "source" ? selectors : null;
      return root.querySelector(selector);
    },
    querySelectorAll() { return []; },
    getElementById(id) {
      if (["source-draft", "memory-body"].includes(id)) return text;
      if (id === "memory-metadata") return metadata;
      return root.querySelector("#" + id);
    },
    createElement() { return new Element(); },
  };
  vm.runInNewContext(script, {
    document, window, URL, URLSearchParams,
    FormData: function (form) { return Object.entries(form.values); },
    async fetch(url, options) {
      const body = JSON.parse(options.body);
      requests.push({ url, body });
      if (url.endsWith("/save") && saveGate) await saveGate;
      if (url.endsWith("/save") && saveReply) return saveReply;
      if (fail) return { ok: false, json: async () => ({ error: "conflict" }) };
      if (url.endsWith("/close")) {
        if (closeGate) await closeGate;
        return { ok: true, json: async () => closeResult };
      }
      revision++;
      const result = kind === "source"
        ? { status: "saved", head: "head-" + revision,
          document: { id: "db", kind: "backend",
            path: "procedures/memory/storage-backends/db.md", sha256: "hash-" + revision },
           workspace: { pull_url: "https://github.com/owner/repo/pull/1", pull_number: 1 },
           rendered: { db: "DB: " + body.draft, markdown: "Markdown: " + body.draft },
          diff: "" }
        : { saved: body.action, edit_context: { uuid: "uuid", tenant: "tenant", revision: "revision-" + revision },
          metadata: body.action === "metadata" ? body.draft : { title: "before" }, preview: body.draft, diff: "" };
      return { ok: true, json: async () => result };
    },
  }, { filename: scriptPath });
  return {
    root, text, metadata, buttons, requests, navigations, selectors, currentView, documentPaths, prLink,
    confirm(value) { confirm = value; },
    confirmations() { return confirmCount; },
    async close() { await close.fire("click"); },
    fail(value) { fail = value; },
    holdClose() {
      let release;
      closeGate = new Promise((resolve) => { release = resolve; });
      return release;
    },
    closeResult(value) { closeResult = value; },
    saveReply(value) { saveReply = value; },
    holdSave() {
      let release;
      saveGate = new Promise((resolve) => { release = resolve; });
      return release;
    },
    async click(area, action) { await buttons[area + "-" + action].fire("click"); },
    async edit(value) { text.value = value; await text.fire("input"); },
    async select(name, value) {
      selectors.values[name] = value;
      await selectors.fire("change", { target: { matches: (tag) => tag === "select" } });
    },
    async returnToPage() { await window.fire("pageshow", { persisted: true }); },
    dirty() {
      let prevented = false;
      window.listeners.beforeunload({ preventDefault() { prevented = true; } });
      return prevented;
    },
  };
}

test("source delayed save shows progress, blocks duplicate requests and retains draft", async () => {
  const app = editor("source", "original");
  await app.edit("edited");
  const release = app.holdSave();
  const saving = app.click("source", "save");
  assert.match(app.buttons["source-save"].innerHTML, /spinner.*Saving…/);
  assert.equal(app.buttons["source-save"].disabled, true);
  assert.equal(app.text.value, "edited");
  await app.click("source", "save");
  assert.equal(app.requests.length, 1);
  release();
  await saving;
  assert.equal(app.dirty(), false);
});

test("source save shows full multiline diagnostics and retains the draft", async () => {
  const app = editor("source", "original");
  await app.edit("edited");
  const details = "line\n" + "x".repeat(1200) + "\nfinal line";
  app.saveReply({ ok: false, json: async () => ({
    error: "GitHub refused the request.", uncertain: false,
    diagnostic: { upstream_status: 422, stage: "commit", details },
  }) });
  await app.click("source", "save");
  const status = app.root.querySelector("[data-workspace]").querySelector("[data-status]").textContent;
  assert.match(status, /Save refused/);
  assert.match(status, /GitHub HTTP status: 422/);
  assert.ok(status.includes(details));
  assert.equal(app.dirty(), true);
});

test("lost save response offers status check without inventing an HTTP status", async () => {
  const app = editor("source", "original");
  await app.edit("edited");
  app.saveReply({ ok: false, json: async () => ({
    error: "GitHub write timed out.", uncertain: true,
    diagnostic: { upstream_status: null, stage: "commit", details: null },
  }) });
  await app.click("source", "save");
  const actions = app.root.querySelector("[data-workspace]");
  assert.match(actions.querySelector("[data-status]").textContent, /outcome unknown/);
  assert.doesNotMatch(actions.querySelector("[data-status]").textContent, /HTTP status/);
  assert.equal(actions.querySelector('[data-workspace-action="reconcile"]').hidden, false);
  assert.equal(actions.querySelector("[data-reconcile-help]").hidden, false);
  assert.equal(app.dirty(), true);
});

test("workspace deletion confirms preservation and cancellation sends nothing", async () => {
  const root = new Element({ workspace: "selected", version: "4" });
  const button = new Element({ workspaceAction: "delete" });
  button.closest = () => root;
  root.groups.set("button", [button]);
  let removed = false;
  root.remove = () => { removed = true; };
  let confirmed = false;
  let confirmation;
  const window = new Element();
  window.confirm = (message) => { confirmation = message; return confirmed; };
  const requests = [];
  const document = {
    querySelector(selector) {
      return selector === 'meta[name="csrf-token"]' ? { content: "token" } : null;
    },
    querySelectorAll(selector) {
      return selector === "[data-workspace-action]" ? [button] : [];
    },
  };
  vm.runInNewContext(script, { document, window, fetch: async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    if (requests.length === 1) return { ok: false, status: 404, json: async () => ({ detail: "Not Found" }) };
    return { ok: true, json: async () => ({ deleted: true }) };
  } }, { filename: scriptPath });
  await button.fire("click");
  assert.match(confirmation, /branch, commits and PR are kept/);
  assert.equal(requests.length, 0);
  assert.equal(removed, false);
  confirmed = true;
  await button.fire("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/procedures/selected/delete");
  assert.equal(requests[0].body.version, 4);
  assert.equal(root.querySelector("[data-status]").textContent, "Delete workspace failed (HTTP 404): Not Found");
  assert.equal(removed, false);
  await button.fire("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(removed, true);
});

for (const kind of ["source", "memory"]) {
  const area = kind === "source" ? "source" : "body";
  for (const original of ["α\r\nβ\r\n", "α\nβ\n", "\rα\rβ\r", "α\r\nβ\nγ\rδ😀"]) {
    test(kind + ": exact baseline, preview/save, successive save " + JSON.stringify(original), async () => {
      const app = editor(kind, original);
      assert.equal(app.text.value, normal(original));
      assert.equal(app.dirty(), false);
      assert.equal(app.buttons[area + "-save"].disabled, true);
      if (kind === "memory") {
        await app.click(area, "preview");
        assert.equal(app.requests.at(-1).body.draft, original);
      }
      const edited = normal(original).replace("α", "A😀");
      await app.edit(edited);
      assert.equal(app.dirty(), true);
      assert.equal(app.buttons[area + "-save"].disabled, false);
      const expected = original.replace("α", "A😀");
      if (kind === "memory") {
        await app.click(area, "preview");
        assert.equal(app.requests.at(-1).body.draft, expected);
      }
      assert.equal(app.dirty(), true);
      await app.click(area, "save");
      assert.equal(app.requests.at(-1).body.draft, expected);
      assert.equal(app.text.value, edited);
      assert.equal(app.dirty(), false);
      assert.equal(app.buttons[area + "-save"].disabled, true);
      if (kind === "memory") {
        await app.click(area, "preview");
        assert.equal(app.requests.at(-1).body.draft, expected);
      }
      await app.edit(edited.replace("β", "B"));
      await app.click(area, "save");
      assert.equal(app.requests.at(-1).body.draft, expected.replace("β", "B"));
      assert.equal(app.dirty(), false);
      if (kind === "source") {
        assert.match(app.requests.at(-1).body.expected_head, /^head-/);
        assert.match(app.requests.at(-1).body.expected_document_hash, /^hash-/);
        assert.equal(app.requests.at(-1).body.document, "db");
        assert.equal(app.requests.at(-1).body.region, "wrap-up.report");
        assert.equal(app.prLink.hidden, false);
        assert.equal(app.prLink.textContent, "PR #1");
      } else assert.match(app.requests.at(-1).body.expected_revision, /^revision-/);
    });
  }
  test(kind + ": revert and failed save retain baseline and draft", async () => {
    const app = editor(kind, "a\r\nb\n");
    await app.edit("changed\nb\n");
    await app.edit("a\nb\n");
    assert.equal(app.dirty(), false);
    await app.edit("changed\nb\n");
    app.fail(true);
    await app.click(area, "save");
    assert.equal(app.dirty(), true);
    assert.equal(app.text.value, "changed\nb\n");
    app.fail(false);
    await app.click(area, "save");
    assert.equal(app.requests.at(-1).body.draft, "changed\r\nb\n");
    assert.equal(app.dirty(), false);
  });
}

for (const [name, value] of [
  ["procedure", "other flow"], ["backend", "markdown"], ["document", ""], ["revision", "base"],
  ["document", "another"],
]) {
  test("source selectors load automatically on " + name + " change", async () => {
    const app = editor("source", "original");
    await app.select(name, value);
    assert.equal(app.navigations.length, 1);
    const url = new URL(app.navigations[0], "http://localhost");
    assert.equal(url.pathname, "/procedures/workspace");
    const expected = { ...app.currentView, [name]: value };
    assert.deepEqual(Object.fromEntries(url.searchParams), expected);
    assert.deepEqual(app.selectors.values, app.currentView);
    assert.equal(app.confirmations(), 0);
    assert.equal(app.requests.length, 0);
    await app.select("document", "another");
    assert.equal(app.navigations.length, 1);
    await app.returnToPage();
    await app.select("document", "another");
    assert.equal(app.navigations.length, 2);
  });
}

test("source selector cancellation retains the draft and current view; confirmation prompts once", async () => {
  const app = editor("source", "original");
  await app.edit("edited");
  app.confirm(false);
  await app.select("backend", "markdown");
  assert.deepEqual(app.selectors.values, app.currentView);
  assert.equal(app.confirmations(), 1);
  assert.equal(app.navigations.length, 0);
  assert.equal(app.text.value, "edited");
  assert.equal(app.dirty(), true);
  app.confirm(true);
  await app.select("backend", "markdown");
  assert.equal(app.confirmations(), 2);
  assert.equal(app.navigations.length, 1);
  assert.equal(app.dirty(), false); // Approved navigation doesn't prompt a second time.
  assert.equal(app.dirty(), true); // Other departures remain protected.
  assert.equal(app.text.value, "edited");
  assert.equal(app.requests.length, 0);
});

function workspaceCreator(fetch) {
  const form = new Element();
  const button = form.querySelector('button[type="submit"]');
  const repository = form.querySelector("select");
  const status = form.querySelector("[data-status]");
  button.textContent = "Open new workspace";
  repository.value = "core";
  const window = new Element();
  const navigations = [];
  window.location = { assign(url) { navigations.push(url); } };
  const document = {
    querySelector(selector) {
      if (selector === 'meta[name="csrf-token"]') return { content: "token" };
      if (selector === "[data-new-workspace]") return form;
      return null;
    },
    querySelectorAll() { return []; },
  };
  vm.runInNewContext(script, { document, window, fetch }, { filename: scriptPath });
  return { button, repository, status, navigations,
    submit: () => form.fire("submit", formEvent()),
    returnToPage: () => window.fire("pageshow", { persisted: true }) };
}

const workspaceRedirect = "/procedures/" + "a".repeat(32);

test("workspace creation immediately shows progress and rejects duplicates through redirect", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  let requests = 0;
  const app = workspaceCreator(async (url, options) => {
    requests++;
    assert.equal(url, "/procedures/workspaces");
    assert.deepEqual(JSON.parse(options.body), { repository: "core" });
    await gate;
    return { ok: true, json: async () => ({ redirect: workspaceRedirect }) };
  });
  const opening = app.submit();
  assert.equal(app.button.textContent, "Opening workspace…");
  assert.equal(app.button.disabled, true);
  assert.equal(app.repository.disabled, true);
  await app.submit();
  assert.equal(requests, 1);
  release();
  await opening;
  assert.deepEqual(app.navigations, [workspaceRedirect]);
  assert.equal(app.button.textContent, "Opening workspace…");
  assert.equal(app.button.disabled, true);
  await app.submit();
  assert.equal(requests, 1);
  await app.returnToPage();
  assert.equal(app.button.textContent, "Open new workspace");
  assert.equal(app.button.disabled, false);
  assert.equal(app.repository.disabled, false);
  assert.equal(app.status.textContent, "");
  await app.submit();
  assert.equal(requests, 2);
});

test("workspace creation restores controls on error and on an unconfirmed response", async () => {
  const replies = [
    { ok: false, json: async () => ({ error: "Workspace unavailable" }) },
    { ok: true, json: async () => ({}) },
    { ok: true, json: async () => ({ redirect: workspaceRedirect }) },
  ];
  const app = workspaceCreator(async () => replies.shift());
  for (const message of ["Workspace unavailable", "Workspace creation could not be confirmed"]) {
    await app.submit();
    assert.match(app.status.textContent, new RegExp(message));
    assert.equal(app.button.textContent, "Open new workspace");
    assert.equal(app.button.disabled, false);
    assert.equal(app.repository.disabled, false);
    assert.deepEqual(app.navigations, []);
  }
  await app.submit();
  assert.deepEqual(app.navigations, [workspaceRedirect]);
});

test("workspace creation doesn't redirect on a late response after returning via history", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const app = workspaceCreator(async () => gate);
  const abandoned = app.submit();
  await app.returnToPage();
  release({ ok: true, json: async () => ({ redirect: workspaceRedirect }) });
  await abandoned;
  assert.equal(app.button.disabled, false);
  assert.deepEqual(app.navigations, []);
});

test("memory: metadata saves preserve dirty body and its original separator baseline", async () => {
  const app = editor("memory", "a\r\nb\nc\r");
  await app.edit("A\nb\nc\n");
  app.metadata.value = '{"title":"after"}';
  await app.metadata.fire("input");
  await app.click("metadata", "save");
  assert.deepEqual(app.requests.at(-1).body.draft, { title: "after" });
  assert.equal(app.text.value, "A\nb\nc\n");
  assert.equal(app.dirty(), true);
  assert.equal(app.root.querySelector("[data-body-dirty]").textContent, "Unsaved body");
  assert.equal(app.root.querySelector("[data-metadata-dirty]").textContent, "Saved baseline");
  await app.click("body", "preview");
  assert.equal(app.requests.at(-1).body.draft, "A\r\nb\nc\r");
  await app.click("body", "save");
  assert.equal(app.requests.at(-1).body.draft, "A\r\nb\nc\r");
  assert.equal(app.dirty(), false);
  app.metadata.value = '{"title":"still unsaved"}';
  await app.metadata.fire("input");
  await app.edit("A\nB\nc\n");
  await app.click("body", "save");
  assert.equal(app.metadata.value, '{"title":"still unsaved"}');
  assert.equal(app.root.querySelector("[data-body-dirty]").textContent, "Saved baseline");
  assert.equal(app.root.querySelector("[data-metadata-dirty]").textContent, "Unsaved metadata");
});

test("memory close: dirty cancellation sends no POST and retains drafts", async () => {
  const app = editor("memory", "a\r\nb\n");
  await app.edit("changed\nb\n");
  app.confirm(false);
  await app.close();
  assert.equal(app.confirmations(), 1);
  assert.equal(app.requests.length, 0);
  assert.equal(app.navigations.length, 0);
  assert.equal(app.text.value, "changed\nb\n");
  assert.equal(app.dirty(), true);
  assert.equal(app.buttons["body-save"].disabled, false);
});

test("memory close: confirmed POST disables editor and navigates without another dirty prompt", async () => {
  const app = editor("memory", "a\r\nb\n");
  await app.edit("changed\nb\n");
  app.metadata.value = '{"title":"draft"}';
  await app.metadata.fire("input");
  await app.close();
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].url, "/memory-edit/memory/close");
  assert.deepEqual(app.navigations, ["/memory"]);
  assert.equal(app.confirmations(), 1);
  assert.equal(app.root.dataset.closed, "true");
  assert.equal(app.dirty(), false);
  assert.equal(app.text.value, "changed\nb\n");
  assert.equal(app.metadata.value, '{"title":"draft"}');
  assert.equal(app.text.disabled, true);
  assert.equal(app.metadata.disabled, true);
  for (const button of Object.values(app.buttons)) assert.equal(button.disabled, true);
  await app.click("body", "save");
  await app.close();
  assert.equal(app.requests.length, 1);
});

test("memory close: failed POST keeps live controls and both drafts", async () => {
  const app = editor("memory", "a\r\nb\n");
  await app.edit("changed\nb\n");
  app.metadata.value = '{"title":"draft"}';
  await app.metadata.fire("input");
  app.fail(true);
  await app.close();
  assert.equal(app.requests.length, 1);
  assert.equal(app.navigations.length, 0);
  assert.notEqual(app.root.dataset.closed, "true");
  assert.equal(app.dirty(), true);
  assert.equal(app.text.value, "changed\nb\n");
  assert.equal(app.metadata.value, '{"title":"draft"}');
  assert.equal(app.text.disabled, false);
  assert.equal(app.metadata.disabled, false);
  assert.equal(app.buttons["body-save"].disabled, false);
  assert.equal(app.buttons["body-preview"].disabled, false);
  app.fail(false);
  await app.click("body", "save");
  assert.equal(app.requests.at(-1).body.draft, "changed\r\nb\n");
});

test("memory close: clean editor closes without confirmation", async () => {
  const app = editor("memory", "a\r\n");
  await app.close();
  assert.equal(app.confirmations(), 0);
  assert.equal(app.requests[0].url, "/memory-edit/memory/close");
  assert.deepEqual(app.navigations, ["/memory"]);
});

test("memory close: pending confirmation retains draft and rejects duplicate submissions", async () => {
  const app = editor("memory", "a\r\nb\n");
  await app.edit("changed\nb\n");
  const release = app.holdClose();
  const closing = app.close();
  assert.equal(app.requests.length, 1);
  assert.equal(app.dirty(), true);
  assert.equal(app.text.value, "changed\nb\n");
  assert.equal(app.text.disabled, false);
  assert.equal(app.navigations.length, 0);
  await app.close();
  await app.click("body", "save");
  assert.equal(app.requests.length, 1);
  release();
  await closing;
  assert.equal(app.dirty(), false);
  assert.equal(app.text.disabled, true);
  assert.deepEqual(app.navigations, ["/memory"]);
});

test("memory close: unconfirmed success response retains live dirty editor", async () => {
  const app = editor("memory", "a\r\n");
  await app.edit("draft\n");
  app.closeResult({ closed: false });
  await app.close();
  assert.equal(app.dirty(), true);
  assert.equal(app.text.disabled, false);
  assert.equal(app.buttons["body-save"].disabled, false);
  assert.equal(app.navigations.length, 0);
});
