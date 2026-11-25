# Quick Wizard Plan: Procedure workspace and Save feedback

## Plan State
- **Source protocol:** `/quick-wizard` (saved)
- **Approval:** APPROVED
- **Confirmation:** Alvi replied “approved” to implementing the plan and starting an isolated bench, then specified the model choices and delegation criteria.

To resume, read the protocol and this file, check approval, then continue from the first unfinished step.

## Inherited Context
**Parent plan:** None. This change builds on Helheim's section editor (`1a59b47`).

**Pre-planning discussion, 2026-11-25:** Alvi wants to remove “Preview both backends” for now, clear accumulated workspaces, see the editing PR inside the workspace, see Save loading like Open new workspace, and understand failures and recovery without interpreting color. “The error message need to be shown fully too so I can know directly why it fails.” Alvi chose A: remove only the Helheim entry after confirmation; preserve GitHub branches, commits and PRs. Resolve unfinished saves first. Hiding/restoring workspaces was considered and not selected.

## Objective
Make it clear what Save is doing, what GitHub reported, and what to do afterward. Let the owner clean up the workspace list without changing anything on GitHub.

## Specification
- Remove the procedure editor's preview-both action and its results pane. Keep composed reading, the backend selector, section editing and save-time validation. Memory editing is unaffected.
- Show the workspace's linked PR in its header across composed, document, edit and base views. Label it with its number; use the stored GitHub URL and update it immediately on first successful PR creation. Do not imply live PR status from a stored link.
- On Save, show a spinner and **Saving…** immediately, prevent duplicate writes, retain the draft, and restore controls appropriate to the actual outcome.
- Report the operation and outcome first, then the actual upstream HTTP status if known and **the full available error message and validation details**, visibly inline with readable wrapping and line breaks. Preserve these through adapter, route and browser without the existing 1,000-character clipping. Render as text. Do not expose credentials, request headers or submitted source. When GitHub provides no reason, say that explicitly rather than inventing one.
- Distinguish refusal, outcome unknown, and commit saved but PR creation failed. For an unknown outcome show **Check save status** with “Checks whether GitHub created the branch or saved the commit. It won't resend your edit.” Put the action beside the message; explain the result and next action afterward. For a confirmed commit with a missing PR, offer the existing PR-only retry.
- Provide a per-workspace **Delete workspace** action with confirmation stating that its GitHub branch, commits and PR are kept. Refuse deletion while a save or PR recovery is pending. Delete only the explicitly selected entry, never as part of deploying or testing the feature.
- This plan does not merge PR #9 or deploy Helheim.

## Confirmed Decisions
| Decision | Mechanism | Reason |
|---|---|---|
| Local deletion | Guarded, version-checked SQLite deletion via `WorkspaceStore` and a protected write route; no GitHub request | A stale tab must not delete a workspace that has since entered recovery. |
| PR display | Reuse `pull_number`, `pull_url`, the validated GitHub link and the existing Save response | Works across views without adding a remote lookup. |
| Error delivery | Carry GitHub's structured message, validation details, upstream status and save stage separately from Helheim's own HTTP status | A Helheim conflict response must not hide a GitHub 500; a timeout has no upstream status. |

## Success Criteria
- [ ] All six requested UI changes work for composed and section-edit workflows where applicable.
- [ ] A multiline GitHub explanation longer than 1,000 characters remains readable through its final line. A no-response case shows no fabricated HTTP status; a pending PR never reads as an unsaved commit.
- [ ] A second Save click issues no second request. Failure preserves the edit. Local deletion leaves GitHub untouched.
- [ ] Focused tests pass; static quality review and QA checklist handoff are recorded.

## Execution Steps
1. **Workspace navigation:** remove the procedure preview action and show the current PR in the shared workspace header. Verify all view modes, initial creation and reload.
2. **Save and recovery feedback** (crosses: GitHub via `gh`): update `github.py`, `workspaces.py`, `editors.py`, the templates and `editors.js` to preserve diagnostic details and distinguish stages. Exercise a delayed Save, refusal, 5xx, lost response and PR-only failure with owned doubles.
3. **Local deletion** (crosses: SQLite): implement guarded deletion and confirmation. Test cancel, success, concurrent change, pending recovery and an existing PR; assert zero GitHub operations.
4. **Review and QA:** run focused route, adapter and browser checks, review the changed files with `/analyze-code-quality`, and build the manual checklist with `/generate-qa-checklist`. Use disposable workspaces, not Alvi's four.

## Execution Log
Read this plan and its approval before starting. Implement each step, verify it, and record the actual result here.

- [x] 1. Workspace navigation — removed procedure preview controls and pane; shared header shows the stored validated numbered PR link and updates after Save. Changed `procedure.html`, `editors.js`, `test_editor_routes.py`, `editors_newlines.test.cjs`. Verification: `python -m pytest tests/test_editor_routes.py -q` (39 passed); `node --test --test-reporter=dot tests/editors_newlines.test.cjs` (51 passed). Tests cover composed, document, edit and base views after reload and immediate Save update; memory preview remains.
- [x] 2. Save and recovery feedback — `github.py`, `workspaces.py`, `editors.py`, `procedure.html`, `procedures.html`, `editors.js`, `app.css` carry safe GitHub messages, validation fields, HTTP status and operation stage; show saving progress, retained draft, status check and PR-only recovery. Tests: `python -m pytest tests/test_editor_routes.py tests/test_workspaces.py tests/test_github.py -q` (85 passed), including real adapter runner → route 422/503 and hosted-token/source redaction, existing lost-response and PR-only tests; `node --test --test-reporter=dot tests/editors_newlines.test.cjs` (53 passed), including delayed duplicate Save and long multiline display. Synthetic `gh` runner and in-process routes, not GitHub production; bench lifecycle and formal integration-test gate belong to coordinator.
- [x] 3. Local deletion — `WorkspaceStore.delete` uses a version-checked SQLite delete and refuses pending saves or PR recovery; protected POST and workspace-list confirmation remove only the selected local entry. `test_editor_routes.py`, `test_workspaces.py` and `editors_newlines.test.cjs` cover cancel, existing PR, successful deletion, stale version, a concurrent recovery race, pending states and zero GitHub calls. Verification: `python -m pytest tests/test_editor_routes.py tests/test_workspaces.py tests/test_github.py tests/test_editor_javascript.py -q` (91 passed); `node --test --test-reporter=dot tests/editors_newlines.test.cjs` (55 passed); Ruff check of changed Python files passed. Tests use disposable SQLite databases and GitHub doubles. Coordinator owns formal QA bench lifecycle and integration gate.
- [ ] 4. Review and QA — coordinator inspected the implementation diff and independently passed 103 tests across `test_qa_git_outcomes`, `test_qa_hosted_github`, `test_editor_routes`, `test_workspaces` and `test_editor_javascript`. The isolated bench answered its identity/health check. Formal integration DEFINE awaits confirmation: use the existing synthetic GitHub subprocess fixture for a long multiline 5xx/unknown-write response, and disposable real SQLite for version-guarded deletion. Follow with browser verification, static review and the QA checklist. Inspect non-JSON GitHub error handling in review: the adapter currently extracts details only from JSON objects.

## Quality Review
Coordinator inspected the changed adapter, store, routes and browser feedback. No `quality-standard.md` was found. Remaining limitation: non-JSON upstream responses do not expose the response body. Alvi accepted the current UX for release after checking local deletion. This does not constitute completion of the formal wizard review or a full QA walk.

## QA Handoff
- **Delete follow-up:** Alvi's local preview on port 18765 showed new templates with an older running backend that did not register `/delete`. Corrected the action-specific failure message (includes the returned HTTP status; no draft wording). Started an updated isolated preview on port 18766 to preserve the earlier preview's in-memory branch. Verified an actual HTTP create/delete/reload removed only the newly created workspace; five deletion-focused Python tests and the 55 JavaScript tests passed. Alvi wants the Helheim changes merged to `main` at deployment time, not now.
- **Bench:** Built; RESET, INJECT, ACT and OBSERVE are documented.
- **Stack:** Down at planning. `qa/bench.py observe` found no owned runtime in this worktree; the existing bench in the original checkout refused the health connection.
- **Integration coverage:** Approved: start an isolated bench and run the boundary checks. The GitHub fixture is synthetic; it does not exercise GitHub production.
- **Checklist:** [Workspace Save and deletion](../qa/checklists/workspace-save-ux.md). Local delete verified with HTTP and isolated state. Formal checklist walk and additional integration DEFINE not completed before release. On 2026-11-25 Alvi confirmed the preview and instructed “let's merge and deploy then.”
