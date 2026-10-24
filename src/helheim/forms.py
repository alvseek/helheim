"""Turn a tool's JSON schema into form fields, and a form back into arguments.

Scalars get real widgets. Objects and arrays get a JSON textarea, because every
tool on these servers takes flat scalars and a recursive builder would be
machinery for a case that does not occur.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SCALAR_KINDS = {
    "string": "text",
    "integer": "number",
    "number": "number",
    "boolean": "checkbox",
}


@dataclass
class Field:
    name: str
    kind: str
    required: bool
    description: str = ""
    choices: list[str] = field(default_factory=list)
    default: Any = None


def fields_from_schema(schema: dict[str, Any] | None) -> list[Field]:
    """Ordered fields for a tool's ``inputSchema``."""
    if not schema:
        return []
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: list[Field] = []
    for name, spec in properties.items():
        spec = spec or {}
        if isinstance(spec.get("enum"), list):
            kind = "enum"
        else:
            kind = SCALAR_KINDS.get(str(spec.get("type")), "json")
        fields.append(
            Field(
                name=name,
                kind=kind,
                required=name in required,
                description=spec.get("description") or spec.get("title") or "",
                choices=[str(choice) for choice in spec.get("enum") or []],
                default=spec.get("default"),
            )
        )
    return fields


def arguments_from_form(
    fields: list[Field], form: dict[str, str | None]
) -> tuple[dict[str, Any], list[str]]:
    """Assemble tool arguments, returning the values and any parse errors."""
    arguments: dict[str, Any] = {}
    errors: list[str] = []
    for item in fields:
        raw = form.get(item.name)
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            continue
        try:
            if item.kind == "checkbox":
                arguments[item.name] = True
            elif item.kind == "number":
                text = str(raw).strip()
                arguments[item.name] = int(text) if text.lstrip("-").isdigit() else float(text)
            elif item.kind == "json":
                arguments[item.name] = json.loads(str(raw))
            else:
                arguments[item.name] = raw
        except (ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{item.name}: {exc}")
    missing = [item.name for item in fields if item.required and item.name not in arguments]
    for name in missing:
        errors.append(f"{name}: required")
    return arguments, errors
