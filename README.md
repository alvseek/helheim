# Helheim

A local dashboard for calling cloud MCP tools and reading what comes back.

It logs in as you against the authorization server each MCP server names, lists
that server's tools, builds a form from each tool's own input schema, sends the
call, and renders the answer as a collapsible tree with its byte size and
duration. Every call is kept locally so it can be reopened or replayed.

Nothing runs on a server, nothing sits in an agent's path, and nothing is read
from another client's files.

## Status

Under construction. See the plan in `munnin-deploy` at
`plans/2026-10-24-helheim-mcp-inspector.md`.

## Licence

Apache-2.0. See `LICENSE`.
