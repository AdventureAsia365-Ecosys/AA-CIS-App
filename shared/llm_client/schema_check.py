"""AA-659 — check a structured model output against its JSON schema before any caller sees it.

OpenAI strict json_schema enforces the schema server-side; a forced tool on Bedrock Converse does
not, so a model can return a tool input with missing keys or with nested objects encoded as JSON
strings. A malformed result must raise (the stage route then tries its next model), never reach a
caller whose error path would quietly accept it.

Covers the subset used in this repo: object/array/string/integer/number/boolean, properties,
required, additionalProperties=false, items, enum.
"""
from __future__ import annotations

import json


class SchemaMismatch(ValueError):
    pass


_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float),
}


def _conform(value, schema: dict, path: str):
    expected = schema.get("type")
    if expected in ("object", "array") and isinstance(value, str):
        # Seen with forced tools: a nested object returned as its JSON text.
        try:
            value = json.loads(value)
        except ValueError:
            raise SchemaMismatch(f"{path}: expected {expected}, got a non-JSON string")
    if expected:
        py = _TYPES[expected]
        ok = isinstance(value, py) and not (expected in ("integer", "number") and isinstance(value, bool))
        if not ok:
            raise SchemaMismatch(f"{path}: expected {expected}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaMismatch(f"{path}: {value!r} not in {schema['enum']}")
    if expected == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaMismatch(f"{path}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise SchemaMismatch(f"{path}: unexpected keys {sorted(extra)}")
        return {k: (_conform(v, props[k], f"{path}.{k}") if k in props else v) for k, v in value.items()}
    if expected == "array" and "items" in schema:
        return [_conform(v, schema["items"], f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def conform(value, schema: dict):
    """Return `value` coerced where safe (JSON-string -> object/array), or raise SchemaMismatch."""
    return _conform(value, schema, "$")
