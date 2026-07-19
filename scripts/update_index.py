"""Merge one generated upstream-core pack entry into index.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .build_runtime_pack import (
    PackBuildError,
    UPSTREAM_CORE_REPO,
    merge_index_entry,
    read_json,
    write_canonical_json,
)


def update_index(index_path: Path, entry_path: Path, *, replace: bool = False) -> dict:
    if index_path.exists():
        current = read_json(index_path)
    else:
        current = {
            "schema": 1,
            "upstream": {"repo": UPSTREAM_CORE_REPO, "branch": "main"},
            "entries": {},
        }
    entry = read_json(entry_path)
    result = merge_index_entry(current, entry, replace=replace)
    write_canonical_json(index_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        result = update_index(args.index, args.entry, replace=args.replace)
    except PackBuildError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
