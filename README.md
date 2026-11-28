# Helheim

A local dashboard for inspecting MCP calls and editing procedure sources and
existing memory. It runs for one person on loopback, using their existing OAuth
connections and GitHub CLI login.

## Choose the job

| View | What it does | Where a save lands |
|---|---|---|
| Inspector | Lists actual tool schemas, sends calls, renders results and keeps replayable history | The called tool's destination; the inspection is stored locally |
| Procedures | Shows composed instructions or their source documents. Select a backend document to see the sections used by this procedure, or edit a backend section directly where it appears. | One editing branch and draft GitHub PR, never a merge or deployment |
| Memory | Browses metadata, verifies a complete body, edits body and metadata independently | Authenticated Munnin operations in the displayed acting account |

GitHub repository rights and memory invitations are independent. Helheim never
holds a PostgreSQL credential and never executes code fetched from an editing
branch.

## Start it

```sh
uv sync --locked
uv run helheim
```

Open <http://127.0.0.1:8765>. Desktop editing requires a loopback bind; public
interfaces are refused in desktop mode. Browser writes require a matching Host, Origin and browser-bound
CSRF token, including the inspector's Send form. Use the remote MCP/HTTP API,
not Helheim's private UI routes, for non-browser integrations.

### Hosted sign-in development

Hosted mode is incomplete and is not yet a deployment recipe. It is enabled
explicitly with `HELHEIM_MODE=hosted` and requires `HELHEIM_HOSTED_ORIGIN`,
`HELHEIM_HOSTED_ISSUER`, `HELHEIM_HOSTED_CLIENT_ID`,
`HELHEIM_HOSTED_CLIENT_SECRET`, and `HELHEIM_HOSTED_OWNER_SUB`. The origin and
issuer must use HTTPS. The login callback is the origin followed by
`/login/callback`. Missing or invalid settings fail startup. Hosted MCP Connect
uses the same hosted web client but a separate `/auth/mcp/callback` and a
separate resource-bound authorization for each server. The consenting account
must match the dashboard owner. Both hosted flows request `form_post`; a
short-lived, one-time server-side handoff lets the original browser complete
the flow without placing the response in a redirect URL. A provider ignoring
that response mode can still expose its query to an upstream proxy before the
app rejects it. Real issuer behavior and the hosted deployment remain
unverified; do not expose this development state to the internet.

Hosted procedure editing permits explicitly server-configured repositories; the
browser can select among them but cannot add another destination. The deployment
operator must configure the intended repositories privately and scope the
dedicated credential to those repositories.
The credential is supplied by the server through
`HELHEIM_HOSTED_GITHUB_TOKEN`; an absent or invalid value disables GitHub
operations rather than falling back to a local `gh` login. The credential is
not a dashboard login or MCP token. A real, scoped disposable-repository proof
and production provisioning remain separate gates.

## Connect a memory server

Helheim uses a pre-registered Native OAuth client, without a client secret.

1. Create a Native application named `Helheim` in the authorization server.
2. Register `http://127.0.0.1:8765/callback`.
3. Put its client ID in `~/.config/helheim/config.toml`, then use **Connect** in
   the inspector.

```toml
[app]
host = "127.0.0.1"
port = 8765
client_id = "your-client-id"
max_store_bytes = 209715200

[servers.munnin]
url = "https://munnin.lok.quest/mcp"
resource = "https://munnin.lok.quest/mcp"
```

Custom ports need a matching redirect URI. Each MCP server needs its own consent
because the token is bound to its resource identifier. Connecting one server does
not connect another. Additional servers use the same configuration shape.

### Edit existing memory

Choose **Memory**, select a configured server, filter metadata or open a record
by UUID. The editor shows the acting account and resolved record identity.

Save body and Save metadata are separate. Saving either preserves typed drafts
in the other area. Preview and diff do not write anything. Summaries are not
generated automatically; `content_summary` remains authored metadata, with a
1,000-Unicode-character limit enforced by Munnin. Use `null` to clear a metadata
field and `[]` to empty tags. Ownership, type, sharing and lifecycle are not
editable here.

Helheim checks the actual tool schemas before enabling editing. A server without
both revision and tenant preconditions stays read-only. It loads every body page
under one context, reconstructs original source positions and verifies the complete
body's hash before enabling Save. A changed account, revoked invitation or stale
revision refuses the save rather than overwriting someone else's work.

Validation, network and conflict errors leave both browser drafts in place. Open
the latest record in a new tab to inspect its current baseline without destroying
them. **Close editor** releases its server baseline after confirming any unsaved
drafts. Baselines also expire 30 minutes after the last preview/save request.
At most 32 editors are retained; opening another releases the least recently used
editor that is not saving. Expiry leaves the browser draft intact, but preview/save
then asks you to open the latest record in another tab. Typing alone does not
refresh the server lifetime. Unsaved drafts are not promised to survive a browser
or app restart.

## Configure procedure sources

Install `gh` and sign in with `gh auth login`. Configure only repositories you
intend this application to edit:

```toml
[procedure_repositories.framework]
owner = "your-github-owner"
name = "your-framework-repository"
base_ref = "main"
catalog_root = ""
```

`catalog_root` is the directory containing `procedures/`, relative to that repository. A
submodule is not a writable source directory: configure its owning repository instead.
Missing, truncated or unsupported catalogs are refused, including the superseded
fragmented `procedures/assemblies`/`procedures/sections` layout.

Open a workspace and choose a procedure, then its document. **Composed instructions**
offers a DB/Markdown selector. Choose **Edit section** on a backend passage to edit it
in place, or select the backend document to see only the regions used by this procedure.
Procedure and component documents still open as whole Markdown files. The separate
Revision selector switches between the workspace branch and its read-only base.

Preview replaces the complete document in an in-memory snapshot and renders both backends
for the selected procedure; the diff covers the section or document being edited.
For a section edit the server replaces just its source text, keeping other regions and
their markers intact. Save commits that complete
blob only, with an atomic expected-HEAD and full-file-hash precondition, then creates or
reuses the workspace's draft PR. Semantic errors may remain in a saved draft, visibly
failing validation, while structural errors, changed document identity/kind, unknown
document identities and unsafe paths are always refused before any write.

If the commit succeeds but the PR is not confirmed, **Retry PR only** never makes
another source commit. An uncertain write requires **Reconcile save** before a
retry. Existing branch collisions, closed/merged PRs and PRs no longer in draft
state are not silently reset or reused.

Saving is not publication. Review and merge the source PR separately, then follow
the consuming compiler/server's normal package/submodule and release process.
Helheim pins its installed composer independently of the source branch being edited;
it does not download a branch's Python code to render a preview. Hermod can adopt
the package API separately; this feature does not migrate or deploy Hermod.

## Local state and privacy

| Location | Contents |
|---|---|
| `~/.config/helheim/config.toml` | Server/source configuration, OAuth client ID and port |
| `~/.config/helheim/tokens.json` | Per-server access/refresh tokens |
| `~/.config/helheim/helheim.db` | Inspector history and procedure workspace references/revisions |
| Process memory | Memory-editing baselines and immutable Git source caches |
| Browser page | Unsaved text drafts |

`HELHEIM_HOME` moves the local files. Token-file permissions are owner-only where
the platform supports them; Windows also relies on the user-profile permissions.
New editor calls are not inserted into generic inspector history, and procedure
workspace rows do not store source drafts or GitHub credentials.

Inspector calls remain deliberately replayable and byte-capped. Results show
duration, byte size and local `o200k_base` token counts. Section counts are independent,
not additive or provider-billed usage; existing history is measured without being
rewritten. The vocabulary is downloaded on first use, but response contents never
leave Helheim for counting. An unavailable vocabulary does not hide a response.

## Develop and verify

```sh
uv run pytest -q -m "not integration"
uv run pytest -q -m integration
uv run ruff check
```

The integration marker reaches the configured real server only when its required
token exists. Passing unit tests is not proof of a real GitHub write, an issuer
exchange or browser behavior.

The hosted multi-repository regression always runs with disposable synthetic
sources and the owned GitHub CLI fixture. It also reopens a workspace saved by
Helheim commit `4586837`. To corroborate it with pinned Hermod source checkouts,
set `HELHEIM_TEST_PROJECT_SOURCE` and `HELHEIM_TEST_WIZARD_SOURCE` to their
respective checkout roots, then run:

```sh
uv run pytest -q tests/test_hosted_multi_repository.py tests/test_hermod_sources.py
```

The optional source checks skip if either checkout is not supplied; they never
write to those checkouts or to GitHub. Record the checked-out commit IDs with
the results, because the paths alone do not identify a revision.

The editor JavaScript regressions require Node.js 18+ and run automatically from
pytest (with an explicit skip reason if Node is unavailable). Run them directly
with `node --test tests/editors_newlines.test.cjs`; they execute the shipped script
with DOM/fetch fixtures, not a native browser.

For unpublished composer development, build its wheel in its own checkout, install
that wheel explicitly into the test environment, and use `uv run --no-sync` there
while the change is unpublished; the tracked dependency above always names a published
immutable revision. Never commit a dependency path that reaches into one private
deployment's parent or sibling directories.

Helheim requires `procedure-composer==0.2.0` for single-file documents. The Git
source in `pyproject.toml` and the resolved entry in `uv.lock` pin the exact
composer revision; the branch open in the editor does not change that dependency.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
