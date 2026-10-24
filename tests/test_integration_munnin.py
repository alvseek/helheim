"""The one test that reaches a real server.

It uses whatever token Helheim already holds for munnin and skips when there is
none, so a fresh clone runs green without a login. Run it deliberately with
``uv run pytest -m integration`` after connecting munnin.
"""

from __future__ import annotations

import asyncio

import pytest

from helheim import mcp_client
from helheim.auth import Auth, AuthError
from helheim.config import load_settings


@pytest.mark.integration
def test_munnin_lists_tools_and_answers_a_read() -> None:
    settings = load_settings()
    server = settings.server("munnin")
    assert server is not None
    auth = Auth(settings)
    try:
        token = auth.token(server)
    except AuthError as exc:
        pytest.skip(f"no usable munnin token: {exc.detail}")

    tools = asyncio.run(mcp_client.list_tools(server, token))
    names = {tool.name for tool in tools}
    assert "awaken" in names, f"expected awaken among {sorted(names)}"

    if "ping" not in names:
        pytest.skip("this server offers no ping tool to call")

    outcome = asyncio.run(mcp_client.call_tool(server, token, "ping", {}))
    assert outcome.is_error is False
    assert outcome.text, "ping returned nothing"
    assert outcome.duration_ms >= 0
