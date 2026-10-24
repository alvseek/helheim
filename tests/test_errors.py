from __future__ import annotations

import httpx2

from helheim.mcp_client import _describe, _root_cause


def test_a_group_is_opened_to_its_single_cause() -> None:
    inner = RuntimeError("the real problem")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    assert _root_cause(group) is inner


def test_nested_groups_are_opened_all_the_way_down() -> None:
    inner = ValueError("deep")
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
    assert _root_cause(group) is inner


def test_an_unauthorized_response_reads_as_expired() -> None:
    request = httpx2.Request("POST", "https://munnin.example/mcp")
    response = httpx2.Response(401, request=request, json={"error": "unauthorized"})
    error = httpx2.HTTPStatusError("nope", request=request, response=response)
    detail, status = _describe(ExceptionGroup("wrapped", [error]))
    assert "401" in detail
    assert status == "expired"


def test_anything_else_reads_as_unavailable_and_names_its_type() -> None:
    detail, status = _describe(ExceptionGroup("wrapped", [RuntimeError("boom")]))
    assert detail == "RuntimeError: boom"
    assert status == "unavailable"


class _FakeErrorData:
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message


class _FakeMcpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.error = _FakeErrorData(code, message)


def test_a_json_rpc_error_names_its_code() -> None:
    detail, status = _describe(
        ExceptionGroup("wrapped", [_FakeMcpError(-32603, "Server returned an error response")])
    )
    assert "-32603" in detail
    assert "Server returned an error response" in detail
    assert status == "unavailable"
