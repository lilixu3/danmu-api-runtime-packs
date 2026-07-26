"""Withdraw one published core SHA from a signed channel index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .build_runtime_pack import (
    PackBuildError,
    new_channel_index,
    read_json,
    revoke_index_entry,
    trusted_channel,
    write_canonical_json,
)


def revoke(index_path: Path, core_sha: str, *, channel: str) -> dict:
    trusted_channel(channel)
    current = read_json(index_path) if index_path.exists() else new_channel_index(channel)
    result = revoke_index_entry(current, core_sha, channel=channel)
    write_canonical_json(index_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--channel", choices=("stable", "dev"), required=True)
    parser.add_argument("--core-sha", required=True)
    args = parser.parse_args()
    try:
        result = revoke(args.index, args.core_sha, channel=args.channel)
    except PackBuildError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
