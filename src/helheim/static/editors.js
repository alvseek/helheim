(function () {
  "use strict";
  const normal = (text) => text.replace(/\r\n|\r/g, "\n");

  // Textareas expose LF only. Uniform originals need no alignment. For mixed
  // endings, use a longest increasing sequence of lines unique in both texts as
  // monotone anchors (lowest old index wins ties). In each gap, preserve equal
  // prefixes/suffixes, then pair replacements positionally. Repeated lines in
  // an ambiguous gap are replacements, not guessed moves. Anchored/matched and
  // paired lines retain their original separator; additional lines inherit the
  // preceding separator, next original separator, or LF. No recursive diff or
  // edit-distance trace: O(lines log lines) work and O(lines) auxiliary memory.
  // If editing brings a standalone CR directly before LF across an empty line,
  // promote that LF to CRLF: otherwise the two breaks would fuse into one.
  function newlineDraft(original, value) {
    const text = normal(value);
    if (text === normal(original)) return original;
    const endings = original.match(/\r\n|\r|\n/g) || [];
    if (endings.every((ending) => ending === endings[0])) {
      return text.replace(/\n/g, endings[0] || "\n");
    }
    const oldLines = normal(original).split("\n");
    const newLines = text.split("\n");
    const unique = (lines) => {
      const indices = new Map();
      lines.forEach((line, index) => { indices.set(line, indices.has(line) ? -1 : index); });
      return indices;
    };
    const oldIndices = unique(oldLines);
    const newIndices = unique(newLines);
    const candidates = [];
    newLines.forEach((line, index) => {
      const oldIndex = oldIndices.get(line);
      if (oldIndex !== undefined && oldIndex !== -1 && newIndices.get(line) !== -1) {
        candidates.push([oldIndex, index]);
      }
    });
    const tails = [];
    const previous = new Array(candidates.length);
    candidates.forEach(([oldIndex], index) => {
      let low = 0;
      let high = tails.length;
      while (low < high) {
        const middle = Math.floor((low + high) / 2);
        if (candidates[tails[middle]][0] < oldIndex) low = middle + 1;
        else high = middle;
      }
      previous[index] = low ? tails[low - 1] : -1;
      tails[low] = index;
    });
    const matches = [];
    for (let index = tails.length ? tails[tails.length - 1] : -1; index !== -1; index = previous[index]) {
      matches.push(candidates[index]);
    }
    matches.reverse();
    const mapped = new Array(newLines.length);
    let oldStart = 0;
    let newStart = 0;
    for (const [oldIndex, newIndex] of [...matches, [oldLines.length, newLines.length]]) {
      let oldEnd = oldIndex;
      let newEnd = newIndex;
      while (oldStart < oldEnd && newStart < newEnd && oldLines[oldStart] === newLines[newStart]) {
        mapped[newStart++] = endings[oldStart++];
      }
      while (oldStart < oldEnd && newStart < newEnd && oldLines[oldEnd - 1] === newLines[newEnd - 1]) {
        mapped[--newEnd] = endings[--oldEnd];
      }
      for (let offset = 0; offset < Math.min(oldEnd - oldStart, newEnd - newStart); offset++) {
        mapped[newStart + offset] = endings[oldStart + offset];
      }
      if (newIndex < newLines.length) mapped[newIndex] = endings[oldIndex];
      oldStart = oldIndex + 1;
      newStart = newIndex + 1;
    }
    const following = new Array(mapped.length);
    let next;
    for (let index = mapped.length - 1; index >= 0; index--) {
      following[index] = next;
      next = mapped[index] || next;
    }
    let preceding;
    return newLines.map((line, index) => {
      if (index === newLines.length - 1) return line;
      let ending = mapped[index] || preceding || following[index] ||
        endings[0] || "\n";
      if (line === "" && preceding === "\r" && ending === "\n") ending = "\r\n";
      preceding = ending;
      return line + ending;
    }).join("");
  }

  // Also execute this same production mapper directly in the Node regressions.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { normal, newlineDraft };
    return;
  }
  const originToken = document.querySelector('meta[name="csrf-token"]').content;
  let dirty = () => false;
  let refresh = () => {};
  let sourceState = null;

  function report(root, text, bad) {
    const output = root.querySelector("[data-status]");
    if (!output) return;
    output.textContent = text;
    output.className = bad ? "errors" : "notice quiet";
  }

  async function post(url, body) {
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": originToken },
        body: JSON.stringify(body || {}),
      });
    } catch (_) {
      const error = new Error("Connection lost; outcome unconfirmed. Keep this draft.");
      error.uncertain = true;
      throw error;
    }
    let result;
    try {
      result = await response.json();
    } catch (_) {
      const error = new Error("Response unreadable; outcome unconfirmed. Keep this draft.");
      error.uncertain = true;
      throw error;
    }
    if (!result || typeof result !== "object") {
      const error = new Error("Response shape is unsupported; outcome unconfirmed.");
      error.uncertain = true;
      throw error;
    }
    if (!response.ok) {
      let message = result.error || result.detail || "Request did not complete.";
      if (typeof message !== "string") message = JSON.stringify(message);
      const error = new Error(message.slice(0, 1000));
      error.uncertain = Boolean(result.uncertain);
      throw error;
    }
    return result;
  }

  async function pending(root, task) {
    if (root.dataset.busy === "true" || root.dataset.closed === "true") return;
    root.dataset.busy = "true";
    const buttons = Array.from(root.querySelectorAll("button"));
    buttons.forEach((button) => { button.disabled = true; button.setAttribute("aria-busy", "true"); });
    report(root, "Request in progress; drafts remain in place.", false);
    try {
      await task();
    } catch (error) {
      report(root, error.message + " Drafts have not been replaced.", true);
    } finally {
      delete root.dataset.busy;
      buttons.forEach((button) => {
        button.disabled = root.dataset.closed === "true";
        button.removeAttribute("aria-busy");
      });
      refresh();
    }
  }

  function validation(errors) {
    const failed = Object.entries(errors || {}).filter((entry) => entry[1].length);
    return failed.length ? " Draft validation still fails: " +
      failed.map(([backend, names]) => backend + ": " + names.join(", ")).join("; ") + "." : "";
  }

  function workspaceResult(root, result) {
    const state = result.workspace;
    const link = root.querySelector("[data-pr-link]");
    if (link && state.pull_url) {
      try {
        const address = new URL(state.pull_url);
        if (address.protocol === "https:" && address.hostname === "github.com" &&
            !address.username && !address.password) {
          link.href = address.href;
          link.hidden = false;
        }
      } catch (_) { /* An invalid link stays unclickable. */ }
    }
    const retry = root.querySelector('[data-workspace-action="retry-pr"]');
    const reconcile = root.querySelector('[data-workspace-action="reconcile"]');
    if (retry) retry.hidden = !state.pending_pr || Boolean(state.pending_action);
    if (reconcile) reconcile.hidden = !state.pending_action;
    if (sourceState) {
      sourceState.pending = Boolean(state.pending_action || state.pending_pr);
    }
    return result.status === "saved_pr_pending"
      ? "Source saved to the branch. Draft PR not confirmed; retry PR only. " + (result.error || "")
      : "Source saved to the editing branch and draft PR. Not merged or deployed.";
  }

  document.querySelectorAll("[data-workspace-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const root = button.closest("[data-workspace]");
      pending(root, async () => {
        const action = button.dataset.workspaceAction;
        const result = await post("/procedures/" + encodeURIComponent(root.dataset.workspace) + "/" + action);
        if (action === "reconcile") {
          if (sourceState) sourceState.reloadRequired = true;
          workspaceResult(root, { workspace: result.workspace });
          report(root, result.notice, false);
        } else {
          workspaceResult(root, result);
          report(root, result.error
            ? "Source remains saved; draft PR is still not confirmed. " + result.error
            : "Draft PR confirmed; the previously saved source commit was not changed.",
          Boolean(result.error));
        }
      });
    });
  });

  const creator = document.querySelector("[data-new-workspace]");
  if (creator) creator.addEventListener("submit", (event) => {
    event.preventDefault();
    pending(creator, async () => {
      const result = await post("/procedures/workspaces", {
        repository: creator.querySelector("select").value,
      });
      window.location.assign(result.redirect);
    });
  });

  const root = document.querySelector("[data-editor-init]");
  if (root) {
    const state = JSON.parse(root.dataset.editorInit);
    if (state.kind === "source") {
      sourceState = state;
      const textarea = document.getElementById("source-draft");
      // HTML's textarea parser strips an initial newline. Set the actual source
      // value explicitly so opening an editor never manufactures a dirty edit.
      textarea.value = normal(state.original);
      const actions = root.querySelector("[data-workspace]");
      dirty = () => textarea.value !== normal(state.original);
      refresh = () => {
        root.querySelector("[data-source-dirty]").textContent = dirty() ? "Unsaved source" : "Saved baseline";
        root.querySelector('[data-source-action="save"]').disabled =
          !dirty() || state.pending || state.reloadRequired || root.dataset.busy === "true";
      };

      function composed(sections, label) {
        const container = document.querySelector("[data-source-composed]");
        container.replaceChildren();
        sections.forEach((section) => {
          const block = document.createElement("section");
          block.className = "source-section";
          const heading = document.createElement("div");
          heading.className = "source-provenance";
          const name = document.createElement("code");
          name.textContent = section.id + " · " + section.source;
          heading.append(name);
          if (section.backend_applicability.length === 2) {
            const shared = document.createElement("span");
            shared.className = "badge";
            shared.textContent = "Shared";
            heading.append(shared);
          }
          const edit = document.createElement("a");
          edit.className = "button";
          edit.textContent = "Edit source";
          edit.href = "/procedures/" + encodeURIComponent(state.id) + "?procedure=" +
            encodeURIComponent(state.procedure) + "&backend=" + state.backend +
            "&source_id=" + encodeURIComponent(section.id);
          heading.append(edit);
          const content = document.createElement("pre");
          content.className = "editor-preview";
          content.textContent = section.text;
          block.append(heading, content);
          container.append(block);
        });
        document.querySelector("[data-composed-label]").textContent = label;
      }

      textarea.addEventListener("input", refresh);
      root.querySelectorAll("[data-source-action]").forEach((button) => {
        button.addEventListener("click", () => pending(root, async () => {
          const action = button.dataset.sourceAction;
          const submitted = newlineDraft(state.original, textarea.value);
          try {
            const result = await post("/procedures/" + encodeURIComponent(state.id) + "/" + action, {
              procedure: state.procedure, backend: state.backend, source_id: state.source_id,
              draft: submitted, expected_head: state.head, expected_source_hash: state.source_hash,
            });
            if (action === "preview") {
              if (!result.rendered || !result.sections || !result.sections[state.backend]) {
                throw new Error("Preview response is incomplete; nothing was saved.");
              }
              root.querySelector("[data-source-preview]").hidden = false;
              root.querySelector("[data-source-diff]").textContent = result.diff;
              root.querySelector("[data-preview-db]").textContent = result.rendered.db;
              root.querySelector("[data-preview-markdown]").textContent = result.rendered.markdown;
              composed(result.sections[state.backend], "Unsaved combined preview · " + state.backend);
              report(actions, "Preview only; nothing was saved." + validation(result.validation_errors), false);
            } else {
              if (!["saved", "saved_pr_pending"].includes(result.status) ||
                  typeof result.head !== "string" || typeof result.source_hash !== "string" ||
                  !result.workspace || !result.sections || !result.sections[state.backend]) {
                const error = new Error("Save result is incomplete; outcome unconfirmed.");
                error.uncertain = true;
                throw error;
              }
              state.original = submitted;
              state.head = result.head;
              state.source_hash = result.source_hash;
              document.getElementById("source-head").textContent = result.head;
              composed(result.sections[state.backend], "Saved branch composition · " + state.backend + " (not deployed)");
              report(actions, workspaceResult(actions, result) + validation(result.validation_errors),
                Boolean(result.error));
            }
          } catch (error) {
            if (action === "save" && error.uncertain) {
              state.reloadRequired = true;
              actions.querySelector('[data-workspace-action="reconcile"]').hidden = false;
            }
            report(actions, error.message + " This draft is retained.", true);
          }
        }));
      });
    } else {
      const body = document.getElementById("memory-body");
      const fields = document.getElementById("memory-metadata");
      body.value = normal(state.body);
      let bodyBaseline = state.body;
      let metadataBaseline = fields.value;
      const bodyDirty = () => body.value !== normal(bodyBaseline);
      const metadataDirty = () => fields.value !== metadataBaseline;
      dirty = () => bodyDirty() || metadataDirty();
      refresh = () => {
        if (root.dataset.closed === "true") {
          body.disabled = true;
          fields.disabled = true;
          root.querySelectorAll("button").forEach((button) => { button.disabled = true; });
          return;
        }
        root.querySelector("[data-body-dirty]").textContent = bodyDirty() ? "Unsaved body" : "Saved baseline";
        root.querySelector("[data-metadata-dirty]").textContent = metadataDirty() ? "Unsaved metadata" : "Saved baseline";
        root.querySelectorAll('[data-memory-action="save"]').forEach((button) => {
          button.disabled = (button.dataset.area === "body" ? !bodyDirty() : !metadataDirty()) ||
            root.dataset.busy === "true";
        });
      };
      body.addEventListener("input", refresh);
      fields.addEventListener("input", refresh);
      const close = root.querySelector("[data-memory-close]");
      if (close) close.addEventListener("click", () => {
        if (root.dataset.busy === "true" || root.dataset.closed === "true") return;
        if (dirty() && !window.confirm("Close this editor and discard its unsaved drafts?")) return;
        return pending(root, async () => {
          const result = await post("/memory-edit/" + encodeURIComponent(state.id) + "/close");
          if (result.closed !== true) throw new Error("Close was not confirmed; keep this editor open.");
          root.dataset.closed = "true";
          dirty = () => false;
          refresh();
          window.location.assign("/memory");
        });
      });
      root.querySelectorAll("[data-memory-action]").forEach((button) => {
        button.addEventListener("click", () => pending(root, async () => {
          const area = button.dataset.area;
          const rawMetadata = fields.value;
          let draft;
          if (area === "body") {
            draft = newlineDraft(bodyBaseline, body.value);
          } else {
            try { draft = JSON.parse(rawMetadata); }
            catch (_) { throw new Error("Metadata must be valid JSON; fix it and retry."); }
          }
          const result = await post("/memory-edit/" + encodeURIComponent(state.id) + "/" +
            button.dataset.memoryAction, {
              action: area, draft: draft, expected_revision: state.context.revision,
            });
          if (button.dataset.memoryAction === "preview") {
            root.querySelector("[data-memory-preview]").hidden = false;
            root.querySelector("[data-preview-title]").textContent = area + " · read-only preview";
            root.querySelector("[data-memory-diff]").textContent = result.diff;
            root.querySelector("[data-memory-content]").textContent = result.preview;
            report(root, "Preview only; nothing was saved.", false);
          } else {
            if (result.saved !== area || !result.edit_context || !result.metadata ||
                result.edit_context.uuid !== state.context.uuid ||
                result.edit_context.tenant !== state.context.tenant ||
                typeof result.edit_context.revision !== "string") {
              throw new Error("Save result is incomplete; outcome unconfirmed. Keep both drafts.");
            }
            state.context = result.edit_context;
            root.querySelector("[data-memory-revision]").textContent = result.edit_context.revision;
            if (area === "body") {
              bodyBaseline = draft;
            } else {
              metadataBaseline = JSON.stringify(result.metadata, null, 2);
              if (fields.value === rawMetadata) fields.value = metadataBaseline;
            }
            report(root, "Saved " + result.saved + " to Munnin. The other area's draft was retained.", false);
          }
        }));
      });
    }
    refresh();
  }

  window.addEventListener("beforeunload", (event) => {
    if (dirty()) { event.preventDefault(); event.returnValue = ""; }
  });
})();
