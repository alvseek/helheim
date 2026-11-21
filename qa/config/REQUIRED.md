# Helheim QA — configuration inventory

| Input | Use | Boundary |
|---|---|---|
| Current project / `--project` | Locate this repo's bench and package metadata | Package must be Helheim |
| `--state` | Owned runtime/config/database/log files | Default `.qa-state`, or the restricted temp QA namespace |
| Generated owner marker | Run identity and test-only stop nonce | Must match exact root/project; never committed |
| Dynamic loopback port | Serve the real app without collisions | Literal 127.0.0.1 only |
| Locked Python environment | Runtime/dev dependencies | No global package modification |

No production URL, user OAuth token, real client ID, database credential or GitHub
token is needed for the base loop. Boundary fixtures may add only explicitly
owned test peers; they may not fall back to the app's normal remote defaults.

The generated local configuration is the swap-in. Normal deployed/user
configuration is never rewritten or copied into QA state.
