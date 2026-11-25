# Workspace Save and deletion — QA checklist

Source: [approved plan](../../plans/2026-11-25-helheim-workspace-save-ux.md). Use a disposable workspace and the synthetic GitHub fixture for writes.

## Happy path
1. Open a procedure, edit one backend section and press Save. During a delayed response, verify the button reads **Saving…** and does not send twice.
2. Check that the PR number appears immediately and after reload in composed, document and base views. Memory preview remains available; procedure preview does not.
3. Cancel deletion, then confirm it. Reload: only the selected workspace is gone. Its remote commit, branch and PR are unchanged.

## Edge cases
| Check | Automated coverage | Still manual |
|---|---|---|
| Refused or uncertain save | `test_save_preserves_safe_diagnostics_through_adapter_and_route`; delayed Save tests in `editors_newlines.test.cjs` | Read the full multiline message in a narrow viewport; confirm the draft remains editable or recoverable as indicated. |
| Commit succeeded, PR failed | `test_pr_failure_keeps_confirmed_commit_and_adapter_diagnostics` | Retry the PR only and inspect the PR link without resaving. |
| Deletion while another tab saves | `test_delete_refuses_a_recovery_started_between_read_and_delete` | Try a stale card; the conflict must not look like a successful deletion. |
| Delete route unavailable | Workspace deletion test in `editors_newlines.test.cjs` | Verify the message names Delete, shows the HTTP status and has no unrelated draft wording. |

## Result
Not yet formally walked.
