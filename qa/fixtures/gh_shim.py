# fixture: synthetic gh executable for the GitHub adapter's real subprocess boundary
# produces: canned GitHub REST/GraphQL responses with retained state and injected response faults
# fidelity: rung 3 hand-rolled seed; shapes mirror only what helheim.github.GitHub parses
# fidelity-checked: PENDING — hosted credential behavior not compared with real GitHub
# teardown: caller removes the exact owned temp root; this process keeps no other state
"""A tiny stand-in for ``gh api``. It never opens a socket or records credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

REASONS = {
    200: "OK", 201: "Created", 404: "Not Found",
    422: "Unprocessable Entity", 500: "Internal Server Error",
}


def emit(status, payload, reason=None):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    sys.stdout.write(
        f"HTTP/2.0 {status} {reason or REASONS.get(status, 'Error')}\n"
        f"Content-Type: application/json\n\n{body}"
    )
    sys.stdout.flush()


def load():
    path = Path(os.environ["HELHEIM_QA_GH_STATE"])
    return json.loads(path.read_text(encoding="utf-8")), path


def persist(state, path):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)


def parse(argv):
    method, endpoint, wants_input, index = "GET", None, False, 0
    while index < len(argv):
        item = argv[index]
        if item == "--method":
            method = argv[index + 1]
            index += 2
        elif item == "--input":
            wants_input = True
            index += 2
        elif item == "--hostname":
            index += 2
        elif item == "--include":
            index += 1
        else:
            endpoint = item
            index += 1
    return method, endpoint, wants_input


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def commit_payload(commit):
    return {
        "tree": {"sha": commit["tree"]},
        "parents": [{"sha": sha} for sha in commit["parents"]],
        "message": commit["message"],
    }


def tree_payload(tree):
    entries = []
    for directory in ("procedures",):
        entries.append({"path": directory, "mode": "040000", "type": "tree", "sha": "0" * 40})
    for path, blob in sorted(tree.items()):
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})
    return {"truncated": False, "tree": entries}


def apply_commit(state, branch, path, raw, headline):
    head = state["branches"][branch]
    tree = dict(state["trees"][state["commits"][head]["tree"]])
    blob = digest(raw)
    state["blobs"][blob] = base64.b64encode(raw).decode("ascii")
    tree[path] = blob
    number = state["next_oid"]
    state["next_oid"] += 1
    oid = f"{number:040x}"
    tree_id = f"{number + 1000:040x}"
    state["trees"][tree_id] = tree
    state["commits"][oid] = {"tree": tree_id, "parents": [head], "message": headline}
    state["branches"][branch] = oid
    state["counters"]["commit"] += 1
    return oid


def record_call(state, method, endpoint, payload):
    call = {"method": method, "endpoint": endpoint}
    if endpoint == "graphql" and isinstance(payload, dict):
        change = payload.get("variables", {}).get("input", {})
        additions = change.get("fileChanges", {}).get("additions", [{}])
        call.update({
            "path": additions[0].get("path") if additions else None,
            "branch": change.get("branch", {}).get("branchName"),
            "repository": change.get("branch", {}).get("repositoryNameWithOwner"),
            "expected_head": change.get("expectedHeadOid"),
        })
    elif isinstance(payload, dict):
        call.update({key: payload[key] for key in ("ref", "sha", "head", "base", "draft")
                     if key in payload})
    state["calls"].append(call)


def main():
    root, path = load()
    method, endpoint, wants_input = parse(sys.argv[1:])
    if endpoint is None:
        emit(400, {"message": "fixture expected an api endpoint"})
        return
    payload = json.loads(sys.stdin.read()) if wants_input else None
    state = root
    if "repositories" in root:
        if endpoint == "graphql":
            name = payload.get("variables", {}).get("input", {}).get("branch", {}).get(
                "repositoryNameWithOwner", ""
            )
        else:
            parts = urlsplit(endpoint).path.lstrip("/").split("/")
            name = "/".join(parts[1:3]) if parts[:1] == ["repos"] else ""
        state = root["repositories"].get(name)
        if state is None:
            emit(404, {"message": "repository not present in fixture"})
            return
    record_call(state, method, endpoint, payload)
    if state.get("hosted_probe"):
        token = os.environ.get("GH_TOKEN", "")
        config = Path(os.environ.get("GH_CONFIG_DIR", ""))
        expected_fingerprint = state.get(
            "credential_fingerprint", hashlib.sha256(b"fixture_only_credential").hexdigest()
        )
        state["calls"][-1]["credential_probe"] = {
            "fixture_token": hashlib.sha256(token.encode()).hexdigest() == expected_fingerprint,
            "alternate_credentials_absent": not any(os.environ.get(key) for key in (
                "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                "HELHEIM_HOSTED_GITHUB_TOKEN", "HELHEIM_HOSTED_CLIENT_SECRET",
                "GH_HOST", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
            )),
            "token_absent_from_argv": bool(token) and all(token not in arg for arg in sys.argv),
            "token_absent_from_input": bool(token) and token not in json.dumps(payload),
            "empty_config": config.is_dir() and not any(config.iterdir()),
            "fixed_hostname": (
                "--hostname" in sys.argv
                and sys.argv[sys.argv.index("--hostname") + 1] == "github.com"
            ),
        }
        if state["faults"].get("credential_refused"):
            persist(root, path)
            emit(401, {"message": "fixture credential refused"})
            return

    prefix = f"repos/{state['owner']}/{state['repo']}/"
    parsed = urlsplit(endpoint)
    route = parsed.path.lstrip("/")
    tail = route[len(prefix):] if route.startswith(prefix) else ""
    fault = None
    reason = None

    if route == "graphql":
        change = payload["variables"]["input"]
        branch = change["branch"]["branchName"]
        expected = change["expectedHeadOid"]
        addition = change["fileChanges"]["additions"][0]
        if state["branches"].get(branch) != expected:
            status, body = 200, {"errors": [{"message": "expected head mismatch"}]}
        elif state["faults"].get("commit") == "conflict":
            status, body = 200, {"errors": [{"message": "commit precondition failed"}]}
        else:
            oid = apply_commit(state, branch, addition["path"],
                               base64.b64decode(addition["contents"]),
                               change["message"]["headline"])
            status, body = 200, {"data": {"createCommitOnBranch": {"commit": {
                "oid": oid, "url": f"https://github.com/{state['owner']}/{state['repo']}"
                                    f"/commit/{oid}",
            }}}}
            fault = state["faults"].get("commit")
    elif tail == f"commits/{state['base_ref']}":
        status, body = 200, {"sha": state["base"]}
    elif tail.startswith("git/ref/heads/"):
        branch = unquote(tail.removeprefix("git/ref/heads/"))
        head = state["branches"].get(branch)
        status, body = ((200, {"object": {"sha": head}}) if head
                        else (404, {"message": "Not Found"}))
    elif tail == "git/refs" and method == "POST":
        branch = payload["ref"].removeprefix("refs/heads/")
        if branch in state["branches"]:
            status, body = 422, {"message": "Reference already exists"}
        else:
            state["branches"][branch] = payload["sha"]
            state["counters"]["branch"] += 1
            status, body = 200, {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
            fault = state["faults"].get("branch")
    elif tail.startswith("git/commits/"):
        commit = state["commits"].get(tail.removeprefix("git/commits/"))
        status, body = ((200, commit_payload(commit)) if commit
                        else (404, {"message": "Not Found"}))
    elif tail.startswith("git/trees/"):
        tree = state["trees"].get(tail.removeprefix("git/trees/"))
        status, body = ((200, tree_payload(tree)) if tree
                        else (404, {"message": "Not Found"}))
    elif tail.startswith("git/blobs/"):
        blob = state["blobs"].get(tail.removeprefix("git/blobs/"))
        status, body = ((200, {"encoding": "base64", "content": blob}) if blob is not None
                        else (404, {"message": "Not Found"}))
    elif tail.startswith("pulls/") and method == "GET":
        number = int(tail.removeprefix("pulls/"))
        fault = state["faults"].get("pull_read")
        after = state["faults"].get("pull_read_after")
        state["counters"]["pull_read"] += 1
        if fault and (after is None or state["counters"]["pull_read"] > after):
            status, body = ((500, {"message": "fixture read failure"}) if fault == "500"
                            else (200, "not-json"))
        else:
            fault = None
            pull = state["pulls"].get(str(number))
            status, body = ((200, pull) if pull else (404, {"message": "Not Found"}))
    elif tail == "pulls" and method == "GET":
        fault = state["faults"].get("pull_list")
        state["counters"]["pull_list"] += 1
        if fault:
            status, body = ((500, {"message": "fixture list failure"}) if fault == "500"
                            else (200, "not-json"))
        else:
            rows = [state["pulls"][key] for key in sorted(state["pulls"])]
            if not state["faults"].get("pull_list_ignore_filter"):
                query = parse_qs(parsed.query)
                head = query.get("head", [None])[0]
                base = query.get("base", [None])[0]
                if head:
                    rows = [row for row in rows
                            if f"{state['owner']}:{row.get('head', {}).get('ref')}" == head]
                if base:
                    rows = [row for row in rows
                            if row.get("base", {}).get("ref") == base]
            status, body = 200, rows
    elif tail == "pulls" and method == "POST":
        number = state["next_pull"]
        state["next_pull"] += 1
        pull = {
            "number": number,
            "html_url": f"https://github.com/{state['owner']}/{state['repo']}/pull/{number}",
            "state": "open", "draft": payload.get("draft", False),
            "head": {"ref": payload["head"]}, "base": {"ref": payload["base"]},
        }
        state["pulls"][str(number)] = pull
        state["counters"]["pull"] += 1
        status, body = 200, pull
    else:
        status, body = 404, {"message": "Not Found"}

    persist(root, path)
    if fault == "lost":
        sys.exit(1)
    if fault == "unreadable":
        emit(200, "not-json")
        return
    emit(status, body, reason)


if __name__ == "__main__":
    main()
