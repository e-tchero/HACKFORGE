"""Minimal YAML emitter for the HACKFORGE_STATE.md source-of-truth file.

Deliberately tiny: it only supports the shapes the state file needs (nested
mappings, lists of scalars, lists of mappings, strings, ints, bools, None).
Using this instead of a hard PyYAML dependency keeps the core installable and
runnable offline.
"""

from __future__ import annotations

from typing import Any

_NEEDS_QUOTES = set(":#{}[],&*!|>'\"%@`")
_RESERVED_PLAIN = {"true", "false", "null", "yes", "no", "on", "off", "~"}


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    if text.strip() != text:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if text.lower() in _RESERVED_PLAIN:
        return f'"{text}"'
    if text[0] in _NEEDS_QUOTES or text[-1] in _NEEDS_QUOTES:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if any(char in text for char in "\n\r\t"):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return text


def _is_container(value: Any) -> bool:
    return isinstance(value, (dict, list, tuple))


def _emit(value: Any, indent: int) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []

    if isinstance(value, dict):
        if not value:
            return [f"{pad}{{}}"]
        for key, item in value.items():
            if _is_container(item) and item:
                lines.append(f"{pad}{key}:")
                lines.extend(_emit(item, indent + 1))
            elif _is_container(item):
                lines.append(f"{pad}{key}: " + ("{}" if isinstance(item, dict) else "[]"))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return lines

    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{pad}[]"]
        for item in value:
            if isinstance(item, dict) and item:
                block = _emit(item, indent + 1)
                first = block[0].lstrip()
                lines.append(f"{pad}- {first}")
                lines.extend(block[1:])
            elif _is_container(item):
                lines.append(f"{pad}-")
                lines.extend(_emit(item, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return lines

    return [f"{pad}{_scalar(value)}"]


def dump(data: dict[str, Any]) -> str:
    """Render a mapping as YAML text (no leading document marker)."""

    return "\n".join(_emit(data, 0)) + "\n"