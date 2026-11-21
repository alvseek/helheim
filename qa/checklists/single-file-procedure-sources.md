# Single-file procedure sources — QA Checklist

**Source**: `munnin-deploy/plans/2026-11-20-munnin-deploy-single-file-procedure-sources.md`
**Purpose**: Catch a delivery mismatch, a write to the wrong file, or a lost draft in the new format.
**Apps under test**: Helheim, the framework compiler/installers, and Munnin's served content. Memory editing and Inspector are regression surfaces; no memory schema migration is part of this change. The original Munnin `procedure-source-editing` checklist remains the record for the earlier editor and release gates.

## Terminology & state model

- **Rendered span**: A piece of composed instructions. Its link opens the owning source, not the span's rendered text.
- **Owning document**: The complete procedure, component or backend Markdown file. Save checks both its baseline hash and the workspace branch head.
- **Base / branch**: The base is read-only. Branch saves can land before the draft PR is confirmed; retrying the PR must not create another commit.

**Key invariants to disprove**

- An edit cannot overwrite another document or a newer revision of the same document.
- A preview, a malformed selection or an unsupported revision cannot create remote state.
- Markdown retains its push status; DB still reports the memory update and open items, but never reports an extra persistence step.

## Happy path — run this first

1. Start the owned local benches. Record the source revision and installed composer revision. Use a disposable workspace backed by the synthetic Git peer; do not use a real repository for writes without separate approval.
2. Open wrap-up in Helheim and compare its Markdown and DB views against the same revision's compiler and local Munnin outputs (M01).
3. Open a referenced backend document, edit a single region, preview both backends, then save. Check the exact blob and draft PR in the peer state (M02).
4. Edit a second region in the same file. Check that the first edit is still present and the same branch and draft PR are reused (M02).

## Automated coverage

| Item | Automated test | Still manual |
|---|---|---|
| A01 — Format and delivery | Framework `tests/test_procedure_composer.py`, `tests/test_converted_workflows.py`, Munnin `tests/content/test_markdown_fidelity.py` | Compare the candidate artifacts and the actual serving endpoints (M01). |
| A02 — Owning document saves and selection fallback | Helheim `test_whole_file_is_the_only_write_target`, `test_stale_document_and_unknown_identity_never_write`, `test_view_falls_back_to_composed_instructions` | Use the real browser controls and compare the draft and committed bytes (M02–M04). |
| A03 — Legacy refusal | Helheim `test_old_format_catalog_and_old_workspace_are_refused_without_deletion` | Verify the user's old workspace remains listed and can still be inspected as state, not edited through the new format (M05). |
| A04 — Partial success and retry | Helheim `test_pull_read_failure_after_commit_is_partial_success`, `test_pull_list_failure_after_commit_is_partial_success` | Check the native status and retry path (M06); the shim is not proof of real GitHub behavior. |

## Manual checks

- [ ] **M01 — Delivery and installed identity.** At the same source revision, compare the compiler's Markdown and DB wrap-up, the Helheim rendered views, and Munnin's DB instructions via local HTTP and MCP. Record the installed composer version and immutable source revision. Markdown must include push status; DB must retain the memory update/open-items instructions without a persistence report. Neither output may leak source markers. The same DB text must appear on both Munnin faces.
- [ ] **M02 — Whole-file save (UI).** Open a procedure, a component and a backend from their spans; compare the editor contents to independent source blobs. For the backend, save edits to two different regions in sequence. Verify only that backend file changed on each commit, both edits survive, and there is one draft PR.
- [ ] **M03 — Navigation fallback (UI).** Open a DB-only document, switch to Markdown with **View**, and check that composed instructions load with a notice and no editable document. Select **Composed instructions only** explicitly; it must load without the stale-selection notice. In another tab, save a newer revision; the stale tab's save must be refused with its draft intact.
- [ ] **M04 — Shared-file impact (UI).** Edit one backend document shared by at least two procedures. Confirm the impact list names both and that a removed referenced region causes structural validation to refuse the draft without a write. Separately remove an operation heading used by the *other* procedure without breaking its region reference: open that procedure to check the semantic warning; absence of a warning on the first procedure does not prove all semantic checks passed.
- [ ] **M05 — Unsupported state.** Open an existing format-1 workspace and attempt to open a format-1 revision in a new workspace. Both must clearly refuse without migration or writes. Confirm the original local workspace record and remote refs are unchanged.
- [ ] **M06 — Partial success and regressions (UI).** In the synthetic peer, fail the PR read after a successful commit. Verify Helheim distinguishes saved source from pending PR; after clearing the fault, **Retry PR only** must not create another commit. Then open Inspector and a disposable memory record to verify the new procedure editor has not broken those views.

Real GitHub writes, real issuer checks, deployed-image checks and global installation require separate approval. Do not count a synthetic run as evidence for any of them.

## Result

*Not yet run. `/run-qa-test --checklist` writes this section — see its Run record.*
