"""Update the schema-1 stable compatibility index used by older App builds."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .build_runtime_pack import (
    PackBuildError,
    UPSTREAM_CORE_REPO,
    read_json,
    validate_channel_source,
    write_canonical_json,
)

PACK_REPO = "lilixu3/danmu-api-runtime-packs"


def update_legacy_index(
    index_path: Path,
    entry_path: Path,
    *,
    replace: bool = False,
) -> dict:
    entry = read_json(entry_path)
    validate_channel_source(
        "stable",
        str(entry.get("coreRepo") or ""),
        str(entry.get("coreBranch") or ""),
    )
    if entry.get("channel") != "stable":
        raise PackBuildError("旧版兼容索引只允许稳定版依赖包")
    core_sha = str(entry.get("coreSha") or "").lower()
    fingerprint = str(entry.get("dependencyFingerprint") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", core_sha):
        raise PackBuildError("旧版兼容 entry 缺少有效核心 SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise PackBuildError("旧版兼容 entry 缺少有效依赖指纹")

    legacy_entry = dict(entry)
    legacy_entry.pop("channel", None)
    legacy_entry.pop("coreBranch", None)
    short_sha = core_sha[:12]
    legacy_entry["artifactUrl"] = (
        f"https://github.com/{PACK_REPO}/releases/download/"
        f"core-{short_sha}/runtime-pack-{short_sha}.zip"
    )

    current = read_json(index_path) if index_path.exists() else {
        "schema": 1,
        "upstream": {"repo": UPSTREAM_CORE_REPO, "branch": "main"},
        "entries": {},
        "dependencyEntries": {},
    }
    if current.get("schema") != 1 or current.get("upstream") != {
        "repo": UPSTREAM_CORE_REPO,
        "branch": "main",
    }:
        raise PackBuildError("旧版兼容索引来源或协议无效")
    entries = dict(current.get("entries") or {})
    previous = entries.get(core_sha)
    if previous is not None and previous != legacy_entry and not replace:
        raise PackBuildError(f"旧版索引同一 SHA 已有不同依赖包：{core_sha}")
    entries[core_sha] = legacy_entry
    current["entries"] = dict(sorted(entries.items()))
    dependency_entries = dict(current.get("dependencyEntries") or {})
    dependency_entries[fingerprint] = core_sha
    current["dependencyEntries"] = dict(sorted(dependency_entries.items()))
    write_canonical_json(index_path, current)
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        result = update_legacy_index(
            args.index,
            args.entry,
            replace=args.replace,
        )
    except PackBuildError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
