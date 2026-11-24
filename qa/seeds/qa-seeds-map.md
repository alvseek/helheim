# QA Seeds Map — Helheim

Last scan: 2026-11-19.

**Category status**: exists-documented  
**Owner**: build-qa-bench

| Material | Location | Grade | Notes |
|---|---|---|---|
| Tiny procedure catalog | [FILES](../../tests/test_workspaces.py) | exists-tribal | Synthetic test prose only, not copied canonical instructions |
| Memory fields/body | [Server](../../tests/test_memory_editor.py) | exists-tribal | Small test data, not a captured live paged read |
| Captured synthetic paged read | [paged-read.json](paged-read.json) | exists-tested | Recorded 2026-11-19 capture of a real Munnin HTTP/MCP response pair; two pages whose display order differs from source order, 101 characters, declared SHA-256 |
| Tiny Git remote state | [git_remote.py](../fixtures/git_remote.py) | exists-tested | Rung 3 hand-rolled branch/commit/PR state around a tiny three-file procedure catalog; response shapes mirror only the adapter's parsed fields |

## Fidelity

The seed records a 2026-11-19 capture of a real Munnin HTTP/MCP response pair and
its own declared body hash. This run reverified that the two pages reconstruct
the declared body exactly and that the peer serves those page objects unchanged
over the real MCP transport. It did not repeat the upstream capture, so rung-1
provenance remains the recorded capture rather than a new live comparison.

## Gaps

Git write-outcome responses are hand-rolled against the adapter's parsed fields,
not captured from a real GitHub repository. Never import the user's memory
corpus, OAuth tokens or normal configuration.
