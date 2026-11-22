# SPDX-License-Identifier: MIT
"""Reading text files that other tools wrote.

Pure logic plus one read. A config or a program is often written by an editor
or a shell that adds a byte-order mark: Notepad writes UTF-8 with a BOM, and
Windows PowerShell 5 redirects as UTF-16. Reading such a file as plain UTF-8
fails with an error that says nothing about the cause, so the encoding is
taken from the BOM when there is one.
"""

from __future__ import annotations

import codecs
from pathlib import Path
from typing import Union

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16: its BOM starts with the UTF-16 LE one
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_text(data: bytes) -> str:
    """Decode by BOM, else as UTF-8. Raises UnicodeDecodeError."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding)
    return data.decode("utf-8")


def read_text(path: Union[str, Path]) -> str:
    """Raises OSError or UnicodeDecodeError."""
    return decode_text(Path(path).read_bytes())
