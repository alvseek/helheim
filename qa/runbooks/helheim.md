# Helheim — QA Runbook

## Goal

Run the actual application against isolated owned state, then execute one
boundary fixture/test at a time.

## Preconditions

Use the locked project environment and [required inputs](../config/REQUIRED.md).
No normal configuration or real OAuth connection is a prerequisite.

## Reset → Clean State

Use the RESET mechanism in the [R/I/A/O index](../README.md#the-riao-loop).
An unowned root, foreign service or reparse-point traversal is an error, not a
reason to force removal.

## Inject → Realistic Data

Use INJECT from the index. It creates empty app storage and safe local
configuration. A boundary fixture supplies its own synthetic/captured specimen;
there is no user-corpus import.

## Daily Loop / Quick Start

Follow the [Daily Loop](../README.md#daily-loop), including STOP when finished.
ACT prints the actual dynamic URL. Observe that owned instance, not a remembered
user-instance port.

## Act → Exercise the System

Run one boundary pair at a time. A pair owns its peer process and temporary
state, so it does not need an earlier ACT service:

```sh
uv run --locked python -m pytest -q tests/test_qa_paged_read.py
```

The paged memory pair opens a literal loopback streamable HTTP/MCP connection to
the owned peer, seeds only [the captured synthetic pages](../seeds/paged-read.json)
and asserts the fixture's write counter stays zero. Do not replace that transport
with an in-process fake. The peer removes its exact `temp/opencode/helheim-qa-*`
root on close; a failed run still needs its owned process checked.

The Git outcome pair runs the actual `helheim.github.GitHub` adapter against an
owned `gh` executable:

```sh
uv run --locked python -m pytest -q tests/test_qa_git_outcomes.py
```

The fixture provides an owned interpreter-backed `gh` shim, restricts `PATH` to
that directory and removes token environment variables, so a missing shim fails
as "gh is not installed" rather than reaching a real login. Do not substitute a
fake `runner` for this subprocess boundary. Its owned temporary root is removed
after close.

## Config Switching

The bench creates an owned home and overrides even import-time default config.
Do not edit `~/.config/helheim`, real token caches or the normal app process.

## Troubleshooting

| Symptom | Action |
|---|---|
| Boot never becomes ready | Inspect owned `.qa-state/runtime/app.log`; do not start a duplicate |
| Ownership/identity mismatch | Stop and investigate; never delete the foreign target |
| OBSERVE fails after STOP | Expected negative smoke result |
| Boundary fixture missing | Build its paired integration test, not a production workaround |

## Known Gotchas

The app module constructs a default instance during import. Explicit factory
settings alone are insufficient isolation: the import environment must be
redirected too, as the worker does.
