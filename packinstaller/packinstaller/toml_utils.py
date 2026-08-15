"""
Utilities for reading and writing /code/admin_panel/config/extra.toml.
"""

from __future__ import annotations

import os
import re
import tomllib
from typing import TypedDict, NotRequired

EXTRA_TOML = "/code/admin_panel/config/extra.toml"


class PackageEntry(TypedDict, total=True):
    location: str
    path: str
    enabled: bool
    editable: NotRequired[bool]


def read_packages() -> list[PackageEntry]:
    """Read all [[ballsdex.packages]] entries from extra.toml."""
    if not os.path.isfile(EXTRA_TOML):
        return []
    with open(EXTRA_TOML, "rb") as f:
        contents = tomllib.load(f)
    return contents.get("ballsdex", {}).get("packages", [])


def _read_raw() -> str:
    if not os.path.isfile(EXTRA_TOML):
        return ""
    with open(EXTRA_TOML) as f:
        return f.read()


def _write_raw(content: str) -> None:
    os.makedirs(os.path.dirname(EXTRA_TOML), exist_ok=True)
    with open(EXTRA_TOML, "w") as f:
        f.write(content)


def package_exists(path: str) -> bool:
    """Return true if a package with the given path is already in extra.toml."""
    return any(p["path"] == path for p in read_packages())


def add_package(entry: PackageEntry, comment: str = "") -> None:
    """
    Append a [[ballsdex.packages]] block to extra.toml.
    """
    raw = _read_raw()
    lines = []
    if comment:
        lines.append(f"\n# {comment}")
    lines.append("[[ballsdex.packages]]")
    lines.append(f'location = "{entry["location"]}"')
    lines.append(f'path = "{entry["path"]}"')
    lines.append(f'enabled = {str(entry["enabled"]).lower()}')
    if entry.get("editable"):
        lines.append("editable = true")
    block = "\n" + "\n".join(lines) + "\n"
    _write_raw(raw + block)


def update_package_location(path: str, new_location: str) -> bool:
    """
    Update the location field for the package with the given path.
    Returns true if the package was found and updated, false if not :(
    Operates on raw text to preserve comments and formatting.
    """
    raw = _read_raw()
    # Find the block containing this path and replace its location line
    pattern = (
        r'(\[\[ballsdex\.packages\]\][^\[]*?)'
        r'(location\s*=\s*"[^"]*")'
        r'([^\[]*?path\s*=\s*"' + re.escape(path) + r'")'
    )

    def replacer(m: re.Match) -> str:
        return m.group(1) + f'location = "{new_location}"' + m.group(3)

    new_raw, count = re.subn(pattern, replacer, raw, flags=re.DOTALL)
    if count == 0:
        return False
    _write_raw(new_raw)
    return True


def remove_package(path: str) -> bool:
    """
    Remove the [[ballsdex.packages]] block for the given path from extra.toml.
    Also removes an immediately preceding comment line if present.
    Returns true if removed, false if not found.
    """
    raw = _read_raw()
    # Match optional comment + block, up to (but not including) the next [[
    pattern = (
        r'\n?(?:#[^\n]*\n)?\[\[ballsdex\.packages\]\][^\[]*?'
        r'path\s*=\s*"' + re.escape(path) + r'"[^\[]*'
    )
    new_raw, count = re.subn(pattern, "", raw, flags=re.DOTALL)
    if count == 0:
        return False
    _write_raw(new_raw)
    return True


def get_package_location(path: str) -> str | None:
    """Return the location URL for a package by path, or none if not found."""
    for p in read_packages():
        if p["path"] == path:
            return p["location"]
    return None
 
