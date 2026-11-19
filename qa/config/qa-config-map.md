# QA Config Map — Helheim

Last scan: 2026-11-19.

**Category status**: exists-documented  
**Owner**: build-qa-bench

| Material | Location | Grade | Notes |
|---|---|---|---|
| QA-only inputs | [Inventory](REQUIRED.md) | exists-documented | Owned root and dynamic loopback port; no real credentials |
| Runtime settings | [Config](../../src/helheim/config.py) | exists-documented | `HELHEIM_HOME`, local configuration and server/source settings |
| Configuration instructions | [README](../../README.md#connect-a-memory-server) | exists-documented | Real OAuth connections; never a QA default |
| Dependency lock | [uv.lock](../../uv.lock) | exists-documented | Reproducible runtime/dev dependencies |

## Gaps

No missing base-loop input. Boundary fixtures must preserve the same isolated
home and endpoint guarantees; no real credentials belong in QA configuration.
