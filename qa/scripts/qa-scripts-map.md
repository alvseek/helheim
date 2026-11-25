# QA Scripts Map — Helheim

Last scan: 2026-11-25; whole-project authored-file scan.

**Category status**: exists-documented  
**Owner**: build-qa-bench

| Role | Location | Grade | Notes |
|---|---|---|---|
| RESET / INJECT / ACT / OBSERVE | [Bench](../bench.py) / [resolver](../README.md#the-riao-loop) | exists-documented | Four explicit subcommands, self-smoked twice |
| ACT | [Entry point](../../src/helheim/__main__.py) | exists-tribal | Documented normal launch, not linked from a QA index |
| OBSERVE | [Health test](../../tests/test_app.py) | exists-tribal | No standalone indexed smoke command |
| Test execution | [Project](../../pyproject.toml) / [README](../../README.md#develop-and-verify) | exists-tribal | Normal test commands are separate from the QA loop resolver |

## Gaps

No further base-loop gap. Per-boundary assertions belong to integration-test
and the caller's feature checklist, not the loop-level health probe.
