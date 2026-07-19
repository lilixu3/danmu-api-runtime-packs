"""Derive a schema-1 stable compatibility pack for older App builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .build_runtime_pack import (
    INDEX_SCHEMA,
    PackBuildError,
    _zip_deterministic,
    canonical_json_bytes,
    read_json,
    sha256_file,
    validate_channel_source,
    write_canonical_json,
)

PACK_REPO = "lilixu3/danmu-api-runtime-packs"
MAX_ENTRIES = 20_000
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    if (
        not name
        or name != name.strip()
        or "\\" in name
        or name.endswith("/")
        or path.is_absolute()
    ):
        return False
    if any(part in ("", ".", "..") for part in name.split("/")):
        return False
    return name in {"manifest.json", "runtime-lock.json"} or name.startswith("node_modules/")


def build_legacy_compat_pack(
    *,
    channel_entry_path: Path,
    channel_archive_path: Path,
    output_dir: Path,
) -> dict:
    entry = read_json(channel_entry_path)
    validate_channel_source(
        "stable",
        str(entry.get("coreRepo") or ""),
        str(entry.get("coreBranch") or ""),
    )
    if entry.get("channel") != "stable":
        raise PackBuildError("旧版兼容包只允许从稳定版通道生成")
    if not channel_archive_path.is_file():
        raise PackBuildError("稳定版通道 ZIP 不存在")
    if channel_archive_path.stat().st_size != int(entry.get("artifactSize") or 0):
        raise PackBuildError("稳定版通道 ZIP 大小与 entry 不一致")
    if sha256_file(channel_archive_path) != entry.get("artifactSha256"):
        raise PackBuildError("稳定版通道 ZIP SHA-256 与 entry 不一致")

    output_dir.mkdir(parents=True, exist_ok=True)
    core_sha = str(entry.get("coreSha") or "").lower()
    fingerprint = str(entry.get("dependencyFingerprint") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", core_sha):
        raise PackBuildError("稳定版 entry 缺少完整核心 SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise PackBuildError("稳定版 entry 缺少有效依赖指纹")
    short_sha = core_sha[:12]
    legacy_archive = output_dir / f"runtime-pack-{short_sha}.zip"

    with tempfile.TemporaryDirectory(prefix="danmu-legacy-pack-") as tmp:
        root = Path(tmp)
        seen: set[str] = set()
        total = 0
        with zipfile.ZipFile(channel_archive_path) as source:
            infos = source.infolist()
            if len(infos) > MAX_ENTRIES:
                raise PackBuildError("稳定版通道 ZIP 条目数量超过上限")
            for info in infos:
                name = info.filename
                mode = (info.external_attr >> 16) & 0xFFFF
                if not _safe_member(name) or name in seen or stat.S_ISLNK(mode):
                    raise PackBuildError(f"稳定版通道 ZIP 路径无效：{name}")
                seen.add(name)
                payload = source.read(info)
                total += len(payload)
                if total > MAX_EXTRACTED_BYTES:
                    raise PackBuildError("稳定版通道 ZIP 解压大小超过上限")
                destination = root / PurePosixPath(name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)

        if "manifest.json" not in seen or "runtime-lock.json" not in seen:
            raise PackBuildError("稳定版通道 ZIP 缺少 manifest 或 runtime lock")
        if not any(name.startswith("node_modules/") for name in seen):
            raise PackBuildError("稳定版通道 ZIP 缺少 node_modules")
        manifest_path = root / "manifest.json"
        if sha256_file(manifest_path) != entry.get("manifestSha256"):
            raise PackBuildError("稳定版 manifest SHA-256 与 entry 不一致")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema") != INDEX_SCHEMA
            or manifest.get("channel") != "stable"
            or manifest.get("coreRepo") != entry.get("coreRepo")
            or manifest.get("coreBranch") != entry.get("coreBranch")
            or manifest.get("coreSha") != entry.get("coreSha")
            or manifest.get("coreVersion") != entry.get("coreVersion")
            or manifest.get("runtimeProtocol") != entry.get("runtimeProtocol")
            or manifest.get("dependencyFingerprint") != fingerprint
            or manifest.get("packages") != entry.get("packages")
        ):
            raise PackBuildError("稳定版 manifest 身份与 entry 不一致")

        legacy_manifest = dict(manifest)
        legacy_manifest["schema"] = 1
        legacy_manifest.pop("channel", None)
        legacy_manifest.pop("coreBranch", None)
        manifest_path.write_bytes(canonical_json_bytes(legacy_manifest))
        _zip_deterministic(root, legacy_archive)

    legacy_entry = dict(entry)
    legacy_entry["artifactUrl"] = (
        f"https://github.com/{PACK_REPO}/releases/download/"
        f"core-{short_sha}/runtime-pack-{short_sha}.zip"
    )
    legacy_entry["artifactSha256"] = sha256_file(legacy_archive)
    legacy_entry["artifactSize"] = legacy_archive.stat().st_size
    # Hash the exact canonical manifest bytes stored in the deterministic ZIP.
    with zipfile.ZipFile(legacy_archive) as archive:
        legacy_entry["manifestSha256"] = hashlib.sha256(
            archive.read("manifest.json")
        ).hexdigest()
    write_canonical_json(output_dir / "legacy-entry.json", legacy_entry)
    return legacy_entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-entry", type=Path, required=True)
    parser.add_argument("--channel-archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build_legacy_compat_pack(
            channel_entry_path=args.channel_entry,
            channel_archive_path=args.channel_archive,
            output_dir=args.output_dir,
        )
    except (PackBuildError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
