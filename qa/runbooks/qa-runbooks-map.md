# QA Runbooks Map — Helheim

Last scan: 2026-11-25; whole-project authored-file scan.

**Category status**: exists-documented  
**Owner**: build-qa-bench

| Material | Location | Grade | Notes |
|---|---|---|---|
| Owned QA loop | [Helheim runbook](helheim.md) | exists-documented | Actual tested launch/reset/observe and configuration isolation |
| App launch | [README](../../README.md#start-it) | exists-documented | Normal user instance, not an isolated test loop |
| Existing test commands | [README](../../README.md#develop-and-verify) | exists-documented | Unit/optional real-server and JavaScript coverage are distinguished |

## Gaps

The base runbook is built. Add boundary-specific fixture/test references as
they become real; never substitute the normal user state for QA preconditions.
