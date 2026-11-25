# QA Instrument Map — Helheim

Last scan: 2026-11-25. Whole-project audit includes Git-listed authored files and
untracked non-ignored material. Shape: one Python 3.11+
FastAPI application, local SQLite state, external MCP/GitHub boundaries; no
submodules, workspace manifest or existing orchestration.

## R/I/A/O loop maturity

| Phase | Grade | Backed by |
|---|---|---|
| RESET | exists-documented | [bench.py](bench.py) `reset`; exact owned runtime only |
| INJECT | exists-documented | [bench.py](bench.py) `inject`; isolated app storage/config |
| ACT | exists-documented | [bench.py](bench.py) `act`; unchanged app factory on loopback |
| OBSERVE | exists-documented | [bench.py](bench.py) `observe`; owned identity and health |

## Index integrity — qa/README.md R/I/A/O table

The [README index](README.md#the-riao-loop) resolves each phase to the CLI's
explicit subcommand. Two actual full cycles passed; stopped OBSERVE failed.

| Phase | Index link | Resolves | Agrees with scan | Status in index |
|---|---|---|---|---|
| RESET | bench.py `reset` | yes | yes — guarded runtime removal | documented |
| INJECT | bench.py `inject` | yes | yes — storage/config initialization | documented |
| ACT | bench.py `act` | yes | yes — owned process launch | documented |
| OBSERVE | bench.py `observe` | yes | yes — identity/health assertions | documented |

## Category maps

| Category | Status | Owner | Map |
|---|---|---|---|
| Runbooks | exists-documented | build-qa-bench | [Runbooks](runbooks/qa-runbooks-map.md) |
| Playbook | missing | build-qa-bench | [Playbook](qa-playbook-map.md) |
| Checklists | exists-documented | generate-qa-checklist | [Checklists](checklists/qa-checklists-map.md) |
| Fixtures | exists-documented | integration-test | [Fixtures](fixtures/qa-fixtures-map.md) |
| Config | exists-documented | build-qa-bench | [Config](config/qa-config-map.md) |
| Scripts | exists-documented | build-qa-bench | [Scripts](scripts/qa-scripts-map.md) |
| Seeds | exists-documented | build-qa-bench | [Seeds](seeds/qa-seeds-map.md) |

## Verdict

The isolated base loop is built and self-smoked. Boundary pairs include the
memory paged read over the fixture's real loopback MCP
transport (20 tests) and Git write outcomes through the adapter's actual gh
subprocess boundary (21 tests, using an owned synthetic shim). A native-browser
subset completed M04, M05, the full paged-fault M08 set and M15 including its
visible-pane screenshots. A real disposable GitHub subset covered M13's
lost/unreadable actions plus ready/closed and actual wrong-base refusals. Fresh
isolated installs at the exact review commit matched the compiler; M16's global
command/consumer portions and real-issuer/deployed guards remain gated. Hosted
login and MCP consent also cross a synthetic HTTPS issuer locally; its fidelity
to real Logto remains pending. Hosted inspector and guarded reads separately
use the captured-page MCP peer with a pre-provisioned test session and token.
Hosted GitHub credential injection is also tested through the owned gh shim;
the real GitHub permission and write proof remains pending.
