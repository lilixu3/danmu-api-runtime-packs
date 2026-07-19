"""Merge one generated channel pack entry into a signed-index source file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .build_runtime_pack import (
    PackBuildError,
    merge_index_entry,
    new_channel_index,
    read_json,
    trusted_channel,
    write_canonical_json,
)


def update_index(
    index_path: Path,
    entry_path: Path,
    *,
    channel: str,
    replace: bool = False,
) -> dict:
    trusted_channel(channel)
    current = read_json(index_path) if index_path.exists() else new_channel_index(channel)
    entry = read_json(entry_path)
    result = merge_index_entry(current, entry, channel=channel, replace=replace)
    write_canonical_json(index_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--channel", choices=("stable", "dev"), required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        result = update_index(
            args.index,
            args.entry,
            channel=args.channel,
            replace=args.replace,
        )
    except PackBuildError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
