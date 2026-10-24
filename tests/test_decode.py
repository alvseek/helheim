from __future__ import annotations

from mcp import types

from helheim.mcp_client import _decode


def text_result(text: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


def test_json_inside_a_text_part_is_parsed() -> None:
    payload, text = _decode(text_result('{"shared": {"reasoning": [1, 2]}}'))
    assert payload == {"shared": {"reasoning": [1, 2]}}
    assert text.startswith("{")
    for key in ("shared", "reasoning"):
        assert key in text


def test_a_plain_string_stays_a_string() -> None:
    payload, text = _decode(text_result("pong"))
    assert payload == "pong"
    assert text == "pong"


def test_several_text_parts_are_joined() -> None:
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="first"),
            types.TextContent(type="text", text="second"),
        ]
    )
    payload, text = _decode(result)
    assert text == "first\nsecond"
    assert payload == "first\nsecond"


def test_structured_content_is_used_when_there_is_no_text() -> None:
    result = types.CallToolResult(content=[], structured_content={"a": 1})
    payload, _ = _decode(result)
    assert payload == {"a": 1}


def test_an_error_result_is_still_decoded() -> None:
    payload, _ = _decode(text_result("boom", is_error=True))
    assert payload == "boom"
