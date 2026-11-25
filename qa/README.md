---
doc_type: qa-riao-readme
---

# Helheim — QA Instrument

> The approved test-only loop: run the real application with owned local state,
> never the user's configuration, OAuth cache or default external connections.

---

## The R/I/A/O Loop

Every QA cycle here is one turn of **RESET → INJECT → ACT → OBSERVE**:

| Phase | What it means in Helheim's approved isolated bench | Mechanism | Status |
|---|---|---|---|
| **RESET** — back to known-clean | Stop only the owned QA worker; clear only marker-guarded runtime state | [bench.py](bench.py) `reset` | documented |
| **INJECT** — realistic data in | Initialize empty app storage and isolated config; seed strategy is synthetic, with boundary specimens added by fixtures | [bench.py](bench.py) `inject` | documented |
| **ACT** — exercise the system | Serve the unchanged app factory on a dynamic loopback port with explicit QA home and no external connections | [bench.py](bench.py) `act` | documented |
| **OBSERVE** — see what happened | Verify owned instance identity and actual `/health`; fail for absent/wrong instance | [bench.py](bench.py) `observe` | documented |

The normal launcher remains unchanged. This missing QA wrapper reuses its app
factory; it does not fork the application's request handlers.

---

## First-Time Setup

**Prerequisites**: Python 3.11+ and this project's locked development environment.
The base bench requires no Docker, real credentials or external service.

1. Run `uv sync --locked` in this repository, or use an already prepared isolated
   project environment.
2. Run the Daily Loop below from the Helheim root.
3. OBSERVE must print the owned service URL/run identity and exit zero.

---

## Daily Loop

```sh
uv run --locked python qa/bench.py reset
uv run --locked python qa/bench.py inject
uv run --locked python qa/bench.py act
uv run --locked python qa/bench.py observe
# Run the boundary tests / controlled fixture scenarios.
uv run --locked python qa/bench.py stop
```

RESET is destructive only inside its exact owned `.qa-state/runtime` child.
Do not point it at user state. `stop` is safe to repeat.

---

## Where Everything Lives

| Layer | What | Where | Built by |
|---|---|---|---|
| App loop | Runbook | [Helheim](runbooks/helheim.md) | /build-qa-bench |
| R/I/A/O mechanisms | One CLI with phase subcommands | [bench.py](bench.py) | /build-qa-bench |
| Required settings | Config inventory | [REQUIRED](config/REQUIRED.md) | /build-qa-bench |
| Test precondition audit | Existing/remaining fixtures | [Fixture map](fixtures/qa-fixtures-map.md) | /integration-test |
| What exists + maturity audit | Map | [qa-map.md](qa-map.md) | /map-qa-instrument |

---

## Config Switching

No normal app file is edited. The worker overrides `HELHEIM_HOME` before importing
the app, then serves an explicit QA-only factory instance. Its configuration
redirects default server URLs to loopback placeholders.

Default state is ignored `./.qa-state`. A custom `--state` is allowed only under
the host temp `opencode/helheim-qa-*` namespace. Ownership markers must match the
exact project/root; symlinks and reparse points are refused.

---

## Known Gaps / Debts

The base loop passed twice on 2026-11-19, and OBSERVE failed after STOP as
expected. An unrelated invalid user-config sentinel was not loaded or modified.
An initial failure exposed import-time default config loading; the worker now
redirects that import before creating the served instance.

- The hosted-login and hosted MCP-consent boundary tests use a disposable,
  hand-rolled HTTPS loopback OIDC issuer with test-local certificate trust,
  two synthetic identities and signed tokens. Its real-Logto fidelity comparison
  is **pending**: neither test proves real Logto behavior, public TLS ingress,
  production registration or a real account's rejection. The MCP-consent test
  doubles protected-resource discovery and MCP peers. Separately, the hosted
  inspector and guarded-memory read use an owned loopback MCP peer and
  pre-provisioned test state; they do not prove a real consent exchange, a real
  deployed Munnin or native-browser behavior. GitHub and Kamal are outside
  these tests. The issuer fixture is [hosted_oidc.py](fixtures/hosted_oidc.py).
- The memory paged-read pair is built and passing over the peer's real loopback
  HTTP/MCP transport: exact source-order reconstruction, 17 corruption/interruption
  cases, a fresh-baseline recovery and a zero fixture write counter. It does not
  prove native-browser presentation, a real issuer, PostgreSQL or GitHub behavior.
- The Git write-outcome pair is built and passing through the adapter's actual
  `gh` subprocess boundary using an owned synthetic shim: lost/unreadable branch
  and commit responses, retained pending intent, reconciliation without
  recommit, partial PR failure and closed/ready/wrong-head/base refusal. It
  does not use a real GitHub remote or native-browser recovery.
- The hosted GitHub credential path is exercised through the same subprocess
  boundary using separate test credentials and synthetic repository selections.
  Its shim is rung 3 and real-GitHub fidelity remains pending; token permissions, expiry and
  the disposable-repository write proof are not established by this test.
- A real `gh` disposable GitHub subset on 2026-11-19 completed M13's
  lost/unreadable branch and commit responses, partial PR-read recovery,
  ready/closed refusals and actual wrong-base metadata refusal on owned
  branches/PRs. Fresh isolated installs at the exact review commit matched the
  compiler. Deployed Munnin schemas still lack the new edit guards, so
  real-issuer/deployed write proof is an explicit release gate; M16's global
  command/consumer portions remain open.
- Real issuer and deployed-version checks are not supplied by this local
  instrument without those approved gates.
- No cross-module playbook: this is one application with test-only remote peers.

---

## Where To Go Next

- Run the application loop → [Helheim runbook](runbooks/helheim.md).
- See **what exists + how mature** → [qa-map.md](qa-map.md).
- **Build** a missing piece → `/build-qa-bench` (rig, runbooks, playbook) or `/integration-test` (fixtures + tests).
- **Run** the verification → `/run-qa-test`.
