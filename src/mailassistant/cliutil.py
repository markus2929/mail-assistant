"""Small helpers shared by the command-line tools. Standard library only."""
from __future__ import annotations

import argparse
import re


def parse_ids(args: list[str]) -> list[int]:
    """'89 90', '89,90', '89, 90' and '#89' all mean [89, 90]. Order kept, duplicates dropped."""
    out: list[int] = []
    for part in re.split(r"[\s,;]+", " ".join(args)):
        part = part.strip().lstrip("#")
        if not part:
            continue
        if not part.isdigit():
            raise argparse.ArgumentTypeError(f"not an id: {part!r}")
        if int(part) not in out:
            out.append(int(part))
    if not out:
        raise argparse.ArgumentTypeError("no id given")
    return out
