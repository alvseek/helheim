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
  async fire(name) { if (this.listeners[name]) await this.listeners[name](); }
}

function editor(kind, original) {
  const state = kind === "source"
    ? { kind, id: "workspace", original, head: "head-0", document_hash: "hash-0",
      procedure: "wrap-up", backend: "db", document_id: "db",
      document_kind: "backend", document_path: "procedures/memory/storage-backends/db.md",
      span: "wrap-up.report" }
    : { kind, id: "memory", body: original,
      context: { uuid: "uuid", tenant: "tenant", revision: "revision-0" } };
  const root = new Element({ editorInit: JSON.stringify(state) });
  const text = new Element();
  const metadata = new Element();
  metadata.value = '{"title":"before"}';
  const buttons = {};
  for (const area of kind === "source" ? ["source"] : ["body", "metadata"]) {
    for (const action of ["preview", "save"]) {
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
  let confirm = true;
  let confirmCount = 0;
  window.confirm = () => { confirmCount++; return confirm; };
  window.location = { assign(url) { navigations.push(url); } };
  let revision = 0;
  let fail = false;
  let closeGate;
  let closeResult = { closed: true };
  const document = {
    querySelector(selector) {
      if (selector === 'meta[name="csrf-token"]') return { content: "token" };
      if (selector === "[data-editor-init]") return root;
      if (selector === "[data-new-workspace]") return null;
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
    document, window, URL,
    async fetch(url, options) {
      const body = JSON.parse(options.body);
      requests.push({ url, body });
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
          workspace: {}, spans: { db: [] }, rendered: { db: body.draft, markdown: body.draft },
          diff: "" }
        : { saved: body.action, edit_context: { uuid: "uuid", tenant: "tenant", revision: "revision-" + revision },
          metadata: body.action === "metadata" ? body.draft : { title: "before" }, preview: body.draft, diff: "" };
      return { ok: true, json: async () => result };
    },
  }, { filename: scriptPath });
  return {
    root, text, metadata, buttons, requests, navigations,
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
    async click(area, action) { await buttons[area + "-" + action].fire("click"); },
    async edit(value) { text.value = value; await text.fire("input"); },
    dirty() {
      let prevented = false;
      window.listeners.beforeunload({ preventDefault() { prevented = true; } });
      return prevented;
    },
  };
}

for (const kind of ["source", "memory"]) {
  const area = kind === "source" ? "source" : "body";
  for (const original of ["α\r\nβ\r\n", "α\nβ\n", "\rα\rβ\r", "α\r\nβ\nγ\rδ😀"]) {
    test(kind + ": exact baseline, preview/save, successive save " + JSON.stringify(original), async () => {
      const app = editor(kind, original);
      assert.equal(app.text.value, normal(original));
      assert.equal(app.dirty(), false);
      assert.equal(app.buttons[area + "-save"].disabled, true);
      await app.click(area, "preview");
      assert.equal(app.requests.at(-1).body.draft, original);
      const edited = normal(original).replace("α", "A😀");
      await app.edit(edited);
      assert.equal(app.dirty(), true);
      assert.equal(app.buttons[area + "-save"].disabled, false);
      await app.click(area, "preview");
      const expected = original.replace("α", "A😀");
      assert.equal(app.requests.at(-1).body.draft, expected);
      assert.equal(app.dirty(), true);
      await app.click(area, "save");
      assert.equal(app.requests.at(-1).body.draft, expected);
      assert.equal(app.text.value, edited);
      assert.equal(app.dirty(), false);
      assert.equal(app.buttons[area + "-save"].disabled, true);
      await app.click(area, "preview");
      assert.equal(app.requests.at(-1).body.draft, expected);
      await app.edit(edited.replace("β", "B"));
      await app.click(area, "save");
      assert.equal(app.requests.at(-1).body.draft, expected.replace("β", "B"));
      assert.equal(app.dirty(), false);
      if (kind === "source") {
        assert.match(app.requests.at(-1).body.expected_head, /^head-/);
        assert.match(app.requests.at(-1).body.expected_document_hash, /^hash-/);
        assert.equal(app.requests.at(-1).body.document, "db");
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
