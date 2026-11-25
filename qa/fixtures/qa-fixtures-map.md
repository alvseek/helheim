# QA Fixtures Map — Helheim

Last scan: 2026-11-25; whole-project authored-file scan.

**Category status**: exists-documented  
**Owner**: integration-test

| Material | Location | Grade | Notes |
|---|---|---|---|
| Memory response builder | [Server](../../tests/test_memory_editor.py) | exists-tribal | Unit test preconditions; not a live MCP transport |
| Procedure/Git world | [FakeGitHub](../../tests/test_workspaces.py) | exists-tribal | Unit test preconditions and atomic-head behavior |
| Application harness | [Route rig](../../tests/test_editor_routes.py) | exists-tribal | In-process HTTP, with fake remote boundaries |
| Browser DOM harness | [JavaScript tests](../../tests/editors_newlines.test.cjs) | exists-tribal | Delivered JS under a modeled DOM, not native browser proof |
| Captured paged memory peer | [paged_remote.py](paged_remote.py) | exists-tested | Owned literal-loopback streamable HTTP/MCP peer over [paged-read.json](../seeds/paged-read.json); valid, single-request, transport-interrupt and pause/resume fault modes; write counter |
| Owned peer lifecycle | [processes.py](processes.py) | exists-tested | One unique temp/opencode state root, nonce identity, marker-verified stop and removal; startup failure cleans its exact child/tree and root |
| Paged-read boundary test | [test_qa_paged_read.py](../../tests/test_qa_paged_read.py) | exists-tested | 20 tests: wire fidelity, exact source-order reconstruction, 17 corruption/interruption cases, fresh recovery |
| Synthetic gh executable | [gh_shim.py](gh_shim.py) | exists-tested | Rung 3; canned REST/GraphQL, retained state and response faults; test-only credential checks record booleans, not values; no socket |
| GitHub outcome peer | [git_remote.py](git_remote.py) | exists-tested | Owns an interpreter-backed `gh` shim and state; `PATH` is restricted to that directory; constructor failure removes only its owned root |
| Git outcome boundary test | [test_qa_git_outcomes.py](../../tests/test_qa_git_outcomes.py) | exists-tested | 21 tests: lost/unreadable branch and commit responses, reconciliation, partial PR failure, PR refusal, served recovery |
| Lifecycle safety test | [test_qa_safety.py](../../tests/test_qa_safety.py) | exists-tested | 5 red-first tests: never-started close, peer startup/constructor cleanup, Git constructor cleanup, bench boot handle cleanup |
| Hosted OIDC peer | [hosted_oidc.py](hosted_oidc.py) | exists-documented | Rung 3, disposable HTTPS loopback issuer, per-test certificate; real Logto fidelity comparison pending |
| Hosted OIDC boundary test | [test_qa_hosted_oidc.py](../../tests/test_qa_hosted_oidc.py) | exists-documented | Synthetic peer only; real-issuer fidelity and production checks remain pending |
| Hosted MCP consent boundary test | [test_qa_hosted_mcp_oidc.py](../../tests/test_qa_hosted_mcp_oidc.py) | exists-documented | Actual Authlib HTTPS to the owned OIDC peer; resource discovery and MCP peers doubled; real-issuer fidelity pending |
| Hosted inspector and guarded reads | [test_qa_hosted_mcp_reads.py](../../tests/test_qa_hosted_mcp_reads.py) | exists-documented | Actual MCP transport to captured-page peer; owner session and resource token pre-provisioned |
| Hosted GitHub credential boundary | [test_qa_hosted_github.py](../../tests/test_qa_hosted_github.py) | exists-documented | Real gh subprocess boundary to owned shim; two separate configured repository/credential pairs, cross-repository refusal; real GitHub fidelity pending |

## Evidence

Paged pair 2026-11-19: 20 passed under both `python -m pytest` and the project's
`pytest` console script. Full non-integration Helheim suite: 179 passed, one
live-server test deselected. The peer's state endpoint reported `writes: 0`
after every mode, and teardown exited zero with its owned root absent and the
port refusing connections. One scoped product-guard mutation made eight paged
tests fail; restoring the exact file byte hash made all 20 pass again.

Git pair 2026-11-19: 21 passed under both runners; full suite with it: 200
passed, one live-server test deselected. The shim recorded real subprocess calls
and retained branch/commit/PR state; every owned root was absent after close.
One scoped guard mutation made four Git tests fail; restoring the exact file
byte hash made all 21 pass again.

Native browser subset 2026-11-19 used both pairs through the served Helheim
pages. It exposed two test-fixture compatibility defects, both fixed: the paged
`query` list was flattened by MCPServer, and an empty Git blob was treated as a
missing blob. Targeted pairs then passed 41; no product file changed.

Lifecycle safety 2026-11-19 added owned-child cleanup for peer startup and bench
boot failures. Red first: 5 safety tests failed; after repair: 5 passed, the
targeted set passed 56, and the full non-integration suite passed 205. Every
failed-run root and child was removed after ownership verification.

Hosted MCP consent 2026-11-25: 13 cases passed twice with the existing nine
dashboard-login issuer tests. Removing the token-storage call made the owner
success case fail; the exact product source hash was restored before rerunning.
Each issuer teardown checked its thread, port and temporary certificate directory.
The rung-3 issuer has not been compared with real Logto.

Hosted MCP reads 2026-11-25: three cases passed twice after a deliberately
empty inspector listing failed the success case; the exact app source hash was
restored. The captured-page peer reported zero writes; each owned process, port
and state root was verified gone. The owner session and resource token were
test-provisioned, not obtained from Logto.

Hosted GitHub credential boundary 2026-11-25: the test-only shim checked the
isolated child environment and fixed host without retaining credential values.
The local branch/expected-HEAD/whole-document/draft-PR flow and refusal cases
were exercised for both selected and disposable synthetic repository
configurations with distinct synthetic credentials, without a real GitHub
repository or credential. Removing the
expected credential made the success case fail; product bytes were restored.
The shim's hosted behavior has not been compared with real GitHub.

Native M04/M05 2026-11-19 reused the prior writable Munnin fixture in a fresh
copy: a 207-character episode paged at 80 bytes, and memory/source CRLF/mixed
split/join/insert/delete/empty/successive saves were checked against independent
persisted bytes. M04 and M05 are complete locally.

Native M08/M15 2026-11-19 drove all 17 paged corruption modes and M15
functional plus visible-pane feedback/Inspector paths through the real Chrome
browse session. M08 and M15 are complete locally with synthetic actors.

## Gaps

Git write-outcome coverage is service/transport level only: no real disposable
GitHub repository, real gh login or native-browser recovery was used. The paged
pair does not prove native-browser presentation, a real issuer or PostgreSQL.
Hosted GitHub credential permissions, expiry and actual GitHub behavior remain
release checks on an approved disposable repository.
Hosted consent and reads do not prove a real issuer, a real deployed Munnin or
the combined real-issuer-to-MCP path.
