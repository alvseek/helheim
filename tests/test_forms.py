from __future__ import annotations

from helheim.forms import arguments_from_form, fields_from_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "agent": {"type": "string", "description": "Which agent to wake"},
        "limit": {"type": "integer"},
        "ratio": {"type": "number"},
        "verbose": {"type": "boolean"},
        "layer": {"type": "string", "enum": ["shared", "agent"]},
        "narrow": {"type": "object"},
    },
    "required": ["agent"],
}


def test_fields_follow_the_schema() -> None:
    fields = fields_from_schema(SCHEMA)
    assert [field.name for field in fields] == [
        "agent",
        "limit",
        "ratio",
        "verbose",
        "layer",
        "narrow",
    ]
    kinds = {field.name: field.kind for field in fields}
    assert kinds == {
        "agent": "text",
        "limit": "number",
        "ratio": "number",
        "verbose": "checkbox",
        "layer": "enum",
        "narrow": "json",
    }
    assert fields[0].required is True
    assert fields[0].description == "Which agent to wake"
    assert fields[4].choices == ["shared", "agent"]


def test_an_empty_schema_has_no_fields() -> None:
    assert fields_from_schema(None) == []
    assert fields_from_schema({}) == []


def test_arguments_are_typed() -> None:
    fields = fields_from_schema(SCHEMA)
    arguments, errors = arguments_from_form(
        fields,
        {
            "agent": "munnin",
            "limit": "5",
            "ratio": "0.5",
            "verbose": "on",
            "layer": "shared",
            "narrow": '{"layer": "identity"}',
        },
    )
    assert errors == []
    assert arguments == {
        "agent": "munnin",
        "limit": 5,
        "ratio": 0.5,
        "verbose": True,
        "layer": "shared",
        "narrow": {"layer": "identity"},
    }


def test_blank_values_are_dropped_not_sent_as_empty() -> None:
    fields = fields_from_schema(SCHEMA)
    arguments, errors = arguments_from_form(fields, {"agent": "munnin", "limit": ""})
    assert errors == []
    assert arguments == {"agent": "munnin"}


def test_a_missing_required_field_is_an_error() -> None:
    fields = fields_from_schema(SCHEMA)
    arguments, errors = arguments_from_form(fields, {})
    assert arguments == {}
    assert "agent: required" in errors


def test_bad_json_is_reported_against_its_field() -> None:
    fields = fields_from_schema(SCHEMA)
    arguments, errors = arguments_from_form(
        fields, {"agent": "munnin", "narrow": "{not json"}
    )
    assert "narrow" not in arguments
    assert any(error.startswith("narrow:") for error in errors)
