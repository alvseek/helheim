# Helheim

A local dashboard for calling cloud MCP tools and reading what comes back.

It logs in as you against the authorization server each MCP server names, lists
that server's tools, builds a form from each tool's own input schema, sends the
call, and renders the answer as a collapsible tree with its byte size and
duration. Every call is kept locally so it can be reopened or replayed.

Nothing runs on a server, nothing sits in an agent's path, and nothing is read
from another client's files. It is a Postman for MCP, for one person, on one
machine.

## What it does

- One page per server, listing the tools that server actually offers right now
- A form generated from each tool's `inputSchema`: text, number, checkbox, enum,
  and a JSON textarea for object and array fields
- The response rendered as nested `<details>` with its byte size and duration, so
  a 130 KB payload is navigable without JavaScript
- Every call stored in local SQLite, reopenable, replayable, and capped in bytes
- Tools that write are labelled. Nothing is blocked.

## Start it

```sh
uv run helheim
```

Then open <http://127.0.0.1:8765>.

## The one manual step: register a client

The authorization server has no registration endpoint, so a client cannot be
created by a program. Without one, nothing can be authorized.

1. In your Logto tenant, create an application of type **Native** called
   `Helheim`. No secret.
2. Add the redirect URI `http://127.0.0.1:8765/callback`.
3. Put its id in `~/.config/helheim/config.toml`:

```toml
[app]
client_id = "your-client-id"
```

If your dashboard listens somewhere else, set `app.port` to match, and register
the matching redirect URI in the tenant.

## Where it keeps things

| Path | What |
|---|---|
| `~/.config/helheim/config.toml` | servers, client id, port, history cap |
| `~/.config/helheim/tokens.json` | one token per server, including refresh tokens |
| `~/.config/helheim/helheim.db` | call history |

Set `HELHEIM_HOME` to move all three somewhere else. The token file is written
with owner-only permissions where the platform honours that; on Windows the
protection is whatever the user profile provides.

Each server needs its own consent, because a token is bound to that server's
resource identifier. Connecting munnin does not connect hermod.

## Configuration

```toml
[app]
host = "127.0.0.1"
port = 8765
client_id = ""
max_store_bytes = 209715200

[servers.munnin]
url = "https://munnin.lok.quest/mcp"
resource = "https://munnin.lok.quest/mcp"
```

`hermod` and `einherjar` follow the same shape. A server whose resource is not
registered in the tenant shows as `refused` rather than breaking the page.

## Tests

```sh
uv run pytest -q                 # unit tests, no network, no token
uv run pytest -q -m integration  # reaches munnin if a token exists, else skips
```

## What it deliberately does not do

- Intercept or record an agent's traffic
- Read another client's local storage
- Guard, confirm or block a call: a write tool writes
- Compare two responses or diff them across time

## Licence

Apache-2.0. See `LICENSE`.
