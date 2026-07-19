"""Build signed-input Android runtime dependency packs for the Danmu App.

The source of truth for core commits is intentionally the official upstream
repository ``huangxd-/danmu_api``.  The pack repository is only a derived,
Android-specific artifact repository; it never becomes a core source mirror.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable


UPSTREAM_CORE_REPO = "huangxd-/danmu_api"
RUNTIME_PROTOCOL = 1
EMBEDDED_NODE_MAJOR = 18
_DISALLOWED_INSTALL_SCRIPTS = {"preinstall", "install", "postinstall"}
_NATIVE_SUFFIXES = {".node", ".so", ".dylib", ".dll"}
_NATIVE_FILENAMES = {"binding.gyp", "binding.cc", "binding.c", "binding.cpp"}


class PackBuildError(RuntimeError):
    """Raised when a dependency tree is not safe for the pure-JS runtime lane."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackBuildError(f"无法读取 JSON：{path}") from exc


def write_canonical_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dependency_fingerprint(dependencies: dict[str, str]) -> str:
    """Return a stable fingerprint for the source core's direct dependency map."""

    return sha256_bytes(canonical_json_bytes(dict(sorted(dependencies.items()))))


def source_dependencies(package_json: dict[str, Any]) -> dict[str, str]:
    """Merge npm dependency declarations in the same precedence order npm uses."""

    merged: dict[str, str] = {}
    for field in ("dependencies", "optionalDependencies"):
        values = package_json.get(field) or {}
        if not isinstance(values, dict):
            raise PackBuildError(f"核心 package.json 的 {field} 不是对象")
        for name, spec in values.items():
            if isinstance(name, str) and isinstance(spec, str) and name.strip() and spec.strip():
                merged[name.strip()] = spec.strip()
    return dict(sorted(merged.items()))


def filter_android_dependencies(
    package_json: dict[str, Any], policy: dict[str, Any]
) -> dict[str, str]:
    """Select the dependency roots that belong in the Android pure-JS pack."""

    all_dependencies = source_dependencies(package_json)
    excluded = policy.get("excludedDirectDependencies") or {}
    if not isinstance(excluded, dict):
        raise PackBuildError("依赖策略 excludedDirectDependencies 必须是对象")
    return {
        name: spec
        for name, spec in all_dependencies.items()
        if name not in excluded
    }


def _package_roots(node_modules_dir: Path) -> Iterable[tuple[Path, str]]:
    """Yield every package root, including nested and scoped packages."""

    if not node_modules_dir.is_dir():
        raise PackBuildError(f"缺少 node_modules：{node_modules_dir}")
    for package_json in sorted(node_modules_dir.rglob("package.json")):
        try:
            relative = package_json.relative_to(node_modules_dir)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) < 2:
            continue
        # The node_modules directory passed to this function is implicit for
        # top-level packages; nested packages carry an explicit marker. A
        # package root has one segment (or two for a scoped package) after the
        # nearest node_modules marker. This excludes arbitrary package data
        # files such as package subdirectories containing their own JSON.
        markers = [index for index, part in enumerate(parts[:-1]) if part == "node_modules"]
        start = markers[-1] + 1 if markers else 0
        package_parts = parts[start:-1]
        if not package_parts:
            continue
        if package_parts[0].startswith("@"):
            if len(package_parts) != 2:
                continue
            package_name = "/".join(package_parts[:2])
        else:
            if len(package_parts) != 1:
                continue
            package_name = package_parts[0]
        package_root = package_json.parent
        yield package_root, package_name


def _iter_runtime_files(node_modules_dir: Path) -> Iterable[tuple[Path, str]]:
    for path in sorted(node_modules_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(node_modules_dir).as_posix()
        if relative == ".package-lock.json" or "/.package-lock.json" in relative:
            continue
        yield path, f"node_modules/{relative}"


def validate_package_tree(node_modules_dir: Path) -> list[dict[str, Any]]:
    """Validate and return package metadata for a pure-JS runtime tree."""

    records: list[dict[str, Any]] = []
    seen_roots: set[Path] = set()
    for package_root, package_name in _package_roots(node_modules_dir):
        if package_root in seen_roots:
            continue
        seen_roots.add(package_root)
        package_file = package_root / "package.json"
        package_json = read_json(package_file)
        if not isinstance(package_json, dict):
            raise PackBuildError(f"包清单不是对象：{package_file}")
        scripts = package_json.get("scripts") or {}
        if not isinstance(scripts, dict):
            raise PackBuildError(f"包 scripts 不是对象：{package_file}")
        bad_scripts = sorted(set(scripts).intersection(_DISALLOWED_INSTALL_SCRIPTS))
        if bad_scripts:
            raise PackBuildError(
                f"拒绝包含安装脚本的包：{package_name}（{', '.join(bad_scripts)}）"
            )
        if package_json.get("os") or package_json.get("cpu"):
            raise PackBuildError(f"拒绝带平台限定的包：{package_name}")
        for file_path, _ in _iter_runtime_files(package_root):
            if file_path.suffix.lower() in _NATIVE_SUFFIXES or file_path.name in _NATIVE_FILENAMES:
                raise PackBuildError(f"拒绝包含原生构建文件的包：{package_name}（{file_path.name}）")
            if "prebuilds" in file_path.parts:
                raise PackBuildError(f"拒绝包含 prebuilds 的包：{package_name}")
        records.append(
            {
                "name": str(package_json.get("name") or package_name),
                "version": str(package_json.get("version") or ""),
                "integrity": None,
            }
        )
    if not records:
        raise PackBuildError("依赖树为空")
    return records


def _lock_package_map(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise PackBuildError("只支持 npm lockfileVersion 2/3 的 packages 字段")
    return {str(key): value for key, value in packages.items() if isinstance(value, dict)}


def collect_package_records(node_modules_dir: Path, lock: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect package versions and SRI values from the generated lockfile."""

    lock_packages = _lock_package_map(lock)
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for package_root, package_name in _package_roots(node_modules_dir):
        if package_root in seen:
            continue
        seen.add(package_root)
        package_json = read_json(package_root / "package.json")
        relative_root = package_root.relative_to(node_modules_dir).as_posix()
        lock_key = f"node_modules/{relative_root}"
        lock_entry = lock_packages.get(lock_key, {})
        record = {
            "name": str(package_json.get("name") or package_name),
            "version": str(package_json.get("version") or ""),
            "integrity": lock_entry.get("integrity"),
            "path": f"node_modules/{relative_root}",
        }
        if not record["version"]:
            raise PackBuildError(f"包缺少版本：{package_root}")
        records.append(record)
    return sorted(records, key=lambda item: (item["path"], item["name"]))


def validate_lockfile_install_scripts(lock: dict[str, Any]) -> None:
    for key, entry in _lock_package_map(lock).items():
        if entry.get("hasInstallScript"):
            raise PackBuildError(f"lockfile 标记了安装脚本：{key}")


def build_file_manifest(node_modules_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path, relative in _iter_runtime_files(node_modules_dir):
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return files


def build_manifest(
    *,
    core_repo: str,
    core_sha: str,
    core_version: str,
    dependency_fingerprint: str,
    node_modules_dir: Path,
    package_records: list[dict[str, Any]],
    runtime_protocol: int = RUNTIME_PROTOCOL,
    node_major: int = EMBEDDED_NODE_MAJOR,
) -> dict[str, Any]:
    if core_repo != UPSTREAM_CORE_REPO:
        raise PackBuildError(f"核心来源不受信任：{core_repo}")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", core_sha):
        raise PackBuildError(f"核心 SHA 不是完整 40 位 commit：{core_sha}")
    return {
        "schema": 1,
        "coreRepo": core_repo,
        "coreSha": core_sha.lower(),
        "coreVersion": core_version,
        "runtimeProtocol": runtime_protocol,
        "nodeMajor": node_major,
        "dependencyFingerprint": dependency_fingerprint,
        "packages": package_records,
        "files": build_file_manifest(node_modules_dir),
    }


def merge_index_entry(
    index: dict[str, Any],
    entry: dict[str, Any],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    if entry.get("coreRepo") != UPSTREAM_CORE_REPO:
        raise PackBuildError(f"拒绝写入非上游核心索引：{entry.get('coreRepo')}")
    core_sha = str(entry.get("coreSha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", core_sha):
        raise PackBuildError(f"索引 entry 缺少完整核心 SHA：{core_sha}")
    result = dict(index) if isinstance(index, dict) else {}
    result.setdefault("schema", 1)
    result.setdefault("upstream", {"repo": UPSTREAM_CORE_REPO, "branch": "main"})
    if result["upstream"].get("repo") != UPSTREAM_CORE_REPO:
        raise PackBuildError("现有索引 upstream 不是官方核心仓库")
    entries = dict(result.get("entries") or {})
    previous = entries.get(core_sha)
    if previous is not None and previous != entry and not replace:
        raise PackBuildError(f"同一核心 SHA 已存在不同依赖包：{core_sha}")
    entries[core_sha] = entry
    result["entries"] = dict(sorted(entries.items()))
    return result


def _copy_core_for_smoke(core_dir: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    source_subdir = core_dir / "danmu_api"
    if not source_subdir.is_dir():
        raise PackBuildError(f"核心缺少 danmu_api 目录：{source_subdir}")
    for source in source_subdir.iterdir():
        destination = target / source.name
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=False)
        else:
            shutil.copy2(source, destination)
    package_json = core_dir / "package.json"
    if package_json.is_file():
        shutil.copy2(package_json, target / "package.json")
    else:
        raise PackBuildError("核心根目录缺少 package.json")
    return target


def run_worker_smoke(core_dir: Path, node_modules_dir: Path, timeout_seconds: int = 60) -> None:
    with tempfile.TemporaryDirectory(prefix="danmu-pack-smoke-") as tmp:
        smoke_core = _copy_core_for_smoke(core_dir, Path(tmp) / "core")
        shutil.copytree(node_modules_dir, smoke_core / "node_modules", symlinks=False)
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "import('./worker.js').then(() => process.exit(0))",
            ],
            cwd=smoke_core,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise PackBuildError(f"worker.js smoke 失败：{detail}")


def _zip_deterministic(source_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [path for path in source_root.rglob("*") if path.is_file()]
    for path in source_root.rglob("*"):
        if path.is_symlink():
            raise PackBuildError(f"依赖包中禁止符号链接：{path}")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files, key=lambda item: item.relative_to(source_root).as_posix()):
            relative = path.relative_to(source_root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def extract_core_version(core_dir: Path) -> str:
    candidates = [core_dir / "danmu_api/configs/globals.js", core_dir / "danmu_api/config/globals.js"]
    pattern = re.compile(r"VERSION\s*:\s*['\"]([^'\"]+)['\"]")
    for path in candidates:
        if not path.is_file():
            continue
        match = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            return match.group(1)
    return ""


def build_pack(
    *,
    core_dir: Path,
    core_repo: str,
    core_sha: str,
    output_dir: Path,
    policy: dict[str, Any],
    artifact_url_base: str = "",
    skip_smoke: bool = False,
    node_major: int = EMBEDDED_NODE_MAJOR,
) -> dict[str, Any]:
    if core_repo != UPSTREAM_CORE_REPO:
        raise PackBuildError(f"只允许构建官方上游核心：{UPSTREAM_CORE_REPO}")
    package_json = read_json(core_dir / "package.json")
    if not isinstance(package_json, dict):
        raise PackBuildError("核心根 package.json 不是对象")
    all_dependencies = source_dependencies(package_json)
    android_dependencies = filter_android_dependencies(package_json, policy)
    if not android_dependencies:
        raise PackBuildError("过滤后没有 Android 运行时依赖")

    output_dir.mkdir(parents=True, exist_ok=True)
    short_sha = core_sha[:12].lower()
    archive_name = f"runtime-pack-{short_sha}.zip"
    with tempfile.TemporaryDirectory(prefix="danmu-pack-build-") as tmp:
        work = Path(tmp)
        project = work / "npm-project"
        project.mkdir()
        write_canonical_json(
            project / "package.json",
            {
                "name": "danmu-api-android-runtime-pack",
                "private": True,
                "type": "module",
                "dependencies": android_dependencies,
            },
        )
        command = [
            "npm",
            "install",
            "--ignore-scripts",
            "--omit=dev",
            "--no-audit",
            "--no-fund",
            "--package-lock=true",
        ]
        result = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-4000:]
            raise PackBuildError(f"npm 解析 Android 依赖失败：{detail}")
        lock = read_json(project / "package-lock.json")
        if not isinstance(lock, dict):
            raise PackBuildError("npm 未生成有效 package-lock.json")
        validate_lockfile_install_scripts(lock)
        node_modules = project / "node_modules"
        validate_package_tree(node_modules)
        records = collect_package_records(node_modules, lock)
        if not skip_smoke:
            run_worker_smoke(core_dir, node_modules)

        pack_root = work / "pack"
        pack_node_modules = pack_root / "node_modules"
        shutil.copytree(
            node_modules,
            pack_node_modules,
            symlinks=False,
            ignore=shutil.ignore_patterns(".bin", ".package-lock.json"),
        )
        # Keep the exact resolver output for diagnosis and future verification.
        shutil.copy2(project / "package-lock.json", pack_root / "runtime-lock.json")
        manifest = build_manifest(
            core_repo=core_repo,
            core_sha=core_sha,
            core_version=extract_core_version(core_dir),
            dependency_fingerprint=dependency_fingerprint(all_dependencies),
            node_modules_dir=pack_node_modules,
            package_records=records,
            node_major=node_major,
        )
        write_canonical_json(pack_root / "manifest.json", manifest)
        archive_path = output_dir / archive_name
        _zip_deterministic(pack_root, archive_path)

    archive_sha = sha256_file(archive_path)
    # The manifest is inside the archive; derive its hash from the generated
    # canonical object so the index can authenticate it without a second file.
    manifest_sha = sha256_bytes(canonical_json_bytes(manifest))
    base = artifact_url_base.rstrip("/")
    artifact_url = f"{base}/{archive_name}" if base else archive_name
    entry = {
        "coreRepo": core_repo,
        "coreSha": core_sha.lower(),
        "coreVersion": manifest["coreVersion"],
        "runtimeProtocol": RUNTIME_PROTOCOL,
        "dependencyFingerprint": manifest["dependencyFingerprint"],
        "artifactUrl": artifact_url,
        "artifactSha256": archive_sha,
        "artifactSize": archive_path.stat().st_size,
        "manifestSha256": manifest_sha,
        "packages": manifest["packages"],
    }
    write_canonical_json(output_dir / "entry.json", entry)
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-dir", type=Path, required=True)
    parser.add_argument("--core-repo", default=UPSTREAM_CORE_REPO)
    parser.add_argument("--core-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--artifact-url-base", default="")
    parser.add_argument("--node-major", type=int, default=EMBEDDED_NODE_MAJOR)
    parser.add_argument("--skip-smoke", action="store_true")
    args = parser.parse_args()
    policy = read_json(args.policy)
    try:
        entry = build_pack(
            core_dir=args.core_dir,
            core_repo=args.core_repo,
            core_sha=args.core_sha,
            output_dir=args.output_dir,
            policy=policy,
            artifact_url_base=args.artifact_url_base,
            skip_smoke=args.skip_smoke,
            node_major=args.node_major,
        )
    except PackBuildError as exc:
        parser.error(str(exc))
    print(json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
