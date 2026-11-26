"""Capture only the known synthetic local record; never refresh/read real user credentials."""
import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from helheim import mcp_client
from helheim.config import Server

UUID = "11111111111141118111111111111111"
HASH = "dd875932a52eeaa68bf3b81fb37d56d28adc2194e1a4160e88407f52c7840658"
TOKEN = "editor-fixture-owner"  # Synthetic fixture credential, never a user's token.


async def capture(base, target):
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("Capture accepts only literal loopback HTTP")

    def get(path):
        with urlopen(Request(base + path, headers={"Authorization": "Bearer " + TOKEN}),
                     timeout=15) as response:
            return json.load(response)

    tenants = get("/api/tenants")
    if not any(row["user_id"] == "fixture-owner" and row["role"] == "owner" for row in tenants):
        raise ValueError("Source is not the synthetic fixture account")
    pages, cursor = [], None
    while True:
        query = {"include_edit_context": "true"}
        if cursor:
            query["cursor"] = cursor
        row = get("/api/record/" + UUID + "?" + urlencode(query))
        assert row["edit_context"]["tenant"] == "fixture-owner"
        assert row["edit_body"]["sha256"] == HASH and row["tags"] == ["fixture"]
        pages.append(row)
        cursor = row["next_cursor"]
        if cursor is None:
            break
    pieces = []
    for row in pages:
        offset = 0
        for segment in row["edit_body"]["segments"]:
            count = segment["characters"]
            pieces.append((segment["source_start"], row["content"][offset:offset + count]))
            offset += count
        assert offset == len(row["content"])
    body = "".join(value for _, value in sorted(pieces))
    assert hashlib.sha256(body.encode()).hexdigest() == HASH
    server = Server("capture", base + "/mcp", base + "/mcp")
    tools = await mcp_client.list_tools(server, TOKEN)
    wire = await mcp_client.call_tool(server, TOKEN, "get_memory", {
        "uuid": UUID, "include_edit_context": True,
    })
    first = wire.payload
    if isinstance(first, dict) and set(first) == {"result"}:
        first = first["result"]
    assert first == pages[0], "HTTP/MCP source capture differs"
    metadata = [row for row in get("/api/query?agent_id=meta") if row["uuid"] == UUID]
    result = {"fidelity": "rung-1 captured synthetic real-Munnin output",
              "captured": datetime.now(UTC).isoformat(), "body": body, "pages": pages,
              "list_memories": metadata, "schemas": {
                  tool.name: tool.input_schema for tool in tools
                  if tool.name in ("get_memory", "list_memories",
                                   "edit_memory_body", "edit_memory_metadata")}}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((json.dumps(result, indent=2, ensure_ascii=True) + "\n").encode())
    print(json.dumps({"captured_pages": len(pages), "body_sha256": HASH,
                      "http_mcp_equal": True, "target": str(target)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(capture(args.source.rstrip("/"), args.output))
