"""Safe dict -> dataclass loading. Never call these on unvalidated data: the validators in safety.py /
editor_view.py run first, so this module can assume the structure is correct."""
from __future__ import annotations

import copy
import dataclasses
import enum
import types
import typing
from typing import Any, get_args, get_origin, get_type_hints


def from_plain(hint: Any, v: Any) -> Any:
    origin = get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        if v is None:
            return None
        return from_plain([a for a in get_args(hint) if a is not type(None)][0], v)
    if dataclasses.is_dataclass(hint):
        hints = get_type_hints(hint)
        return hint(**{f.name: from_plain(hints[f.name], v[f.name]) for f in dataclasses.fields(hint)})
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return hint(v)
    if hint is float:
        return float(v)
    if origin is tuple:
        el = get_args(hint)[0] if get_args(hint) else Any
        return tuple(from_plain(el, x) for x in v)
    if origin is dict or hint is dict or hint is Any:
        return copy.deepcopy(v)
    return v
