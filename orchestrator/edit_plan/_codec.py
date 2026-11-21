"""Strict dict <-> frozen-dataclass codec and JSON Schema export, driven by the dataclass definitions.

One definition feeds three things (typed model, strict loader, JSON Schema), so they cannot drift apart.
Supported field types: str, int (never bool), bool, `X | None`, `tuple[X, ...]`, nested dataclasses and
`dict[str, scalar]` (scalar = str | int | bool; floats are refused so no number is ever ambiguous).
A string field may carry `metadata={"choices": (...)}`; any other value is refused.
"""
from __future__ import annotations

import dataclasses
import types
import typing
from typing import Any, get_args, get_origin, get_type_hints


class PlanSchemaError(ValueError):
    """Structured schema failure. `code` is one of the SCHEMA_* codes, `path` a JSON-pointer-like location."""

    def __init__(self, code: str, path: str, message: str):
        super().__init__(f"{code} at {path or '/'}: {message}")
        self.code, self.path, self.message = code, path, message


def _is_union(hint: Any) -> bool:
    return get_origin(hint) in (typing.Union, types.UnionType)


def load(cls: type, data: Any, path: str = "") -> Any:
    """Build `cls` from a plain dict, refusing unknown fields, missing fields and wrong types."""
    if not isinstance(data, dict):
        raise PlanSchemaError("SCHEMA_BAD_VALUE", path, f"expected an object, got {type(data).__name__}")
    hints = get_type_hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - set(fields), key=str)
    if unknown:
        raise PlanSchemaError("SCHEMA_UNKNOWN_FIELD", path, f"unknown field(s) {unknown}")
    kwargs = {}
    for name, f in fields.items():
        here = f"{path}/{name}"
        if name not in data:
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                raise PlanSchemaError("SCHEMA_MISSING_FIELD", here, "required field is missing")
            continue
        kwargs[name] = _value(hints[name], data[name], here, f.metadata.get("choices"))
    return cls(**kwargs)


def _value(hint: Any, v: Any, path: str, choices: Any = None) -> Any:
    if _is_union(hint):
        if v is None:
            return None
        inner = [a for a in get_args(hint) if a is not type(None)]
        return _value(inner[0], v, path, choices)
    if dataclasses.is_dataclass(hint):
        return load(hint, v, path)
    origin = get_origin(hint)
    if origin is tuple:
        if not isinstance(v, (list, tuple)):
            raise PlanSchemaError("SCHEMA_BAD_VALUE", path, "expected a list")
        el = get_args(hint)[0]
        return tuple(_value(el, x, f"{path}[{i}]") for i, x in enumerate(v))
    if origin is dict:
        if not isinstance(v, dict):
            raise PlanSchemaError("SCHEMA_BAD_VALUE", path, "expected an object")
        for k, x in v.items():
            if not isinstance(k, str) or isinstance(x, float) or not isinstance(x, (str, int, bool)):
                raise PlanSchemaError("SCHEMA_BAD_VALUE", f"{path}/{k}", "values must be str, int or bool (no floats)")
        return dict(v)
    if hint is bool:
        if not isinstance(v, bool):
            raise PlanSchemaError("SCHEMA_BAD_VALUE", path, "expected true/false")
        return v
    if hint is int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise PlanSchemaError("SCHEMA_BAD_VALUE", path, "expected an integer (no floats, no booleans)")
        return v
    if hint is str:
        if not isinstance(v, str):
            raise PlanSchemaError("SCHEMA_BAD_VALUE", path, "expected a string")
        if choices is not None and v not in choices:
            raise PlanSchemaError("SCHEMA_BAD_VALUE", path, f"{v!r} is not one of {list(choices)}")
        return v
    raise PlanSchemaError("SCHEMA_BAD_VALUE", path, f"unsupported field type {hint!r}")  # pragma: no cover


def dump(obj: Any) -> Any:
    """Plain JSON-able form (tuples become lists, dataclasses become dicts)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: dump(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [dump(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): dump(v) for k, v in obj.items()}
    return obj


def json_schema(cls: type, *, defs: dict | None = None, top: bool = True) -> dict:
    """JSON Schema (draft 2020-12) for `cls`; nested dataclasses go under `$defs`."""
    defs = {} if defs is None else defs
    hints = get_type_hints(cls)
    props, required = {}, []
    for f in dataclasses.fields(cls):
        props[f.name] = _schema_of(hints[f.name], defs, f.metadata.get("choices"))
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            required.append(f.name)
    body = {"type": "object", "additionalProperties": False, "properties": props, "required": required}
    if not top:
        return body
    out = {"$schema": "https://json-schema.org/draft/2020-12/schema", **body}
    out["$defs"] = dict(sorted(defs.items()))
    return out


def _schema_of(hint: Any, defs: dict, choices: Any = None) -> dict:
    if _is_union(hint):
        inner = [a for a in get_args(hint) if a is not type(None)][0]
        return {"anyOf": [_schema_of(inner, defs, choices), {"type": "null"}]}
    if dataclasses.is_dataclass(hint):
        defs[hint.__name__] = json_schema(hint, defs=defs, top=False)
        return {"$ref": f"#/$defs/{hint.__name__}"}
    origin = get_origin(hint)
    if origin is tuple:
        return {"type": "array", "items": _schema_of(get_args(hint)[0], defs)}
    if origin is dict:
        return {"type": "object", "additionalProperties": {"type": ["string", "integer", "boolean"]}}
    if hint is bool:
        return {"type": "boolean"}
    if hint is int:
        return {"type": "integer"}
    s: dict = {"type": "string"}
    if choices is not None:
        s["enum"] = list(choices)
    return s
