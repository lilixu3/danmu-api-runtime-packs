"""Build the single signed Android node_modules runtime pack."""

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


PACK_REPO = "lilixu3/danmu-api-runtime-packs"
MANIFEST_SCHEMA = 3
RUNTIME_PROTOCOL = 2
EMBEDDED_NODE_MAJOR = 18
TRUSTED_CORE_LABELS = ("stable", "dev")
EXCLUDED_CORE_DEPENDENCIES = {"chokidar", "dotenv", "esbuild", "redis"}
_DISALLOWED_INSTALL_SCRIPTS = {"preinstall", "install", "postinstall"}
_NATIVE_SUFFIXES = {".node", ".so", ".dylib", ".dll"}
_NATIVE_FILENAMES = {"binding.gyp", "binding.cc", "binding.c", "binding.cpp"}
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$")


class PackBuildError(RuntimeError):
    """Raised when the shared pure-JavaScript runtime cannot be published."""


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
    return sha256_bytes(canonical_json_bytes(dict(sorted(dependencies.items()))))


def _validate_registry_dependency_spec(name: str, spec: str) -> None:
    normalized = spec.strip()
    lowered = normalized.lower()
    non_registry_prefix = re.compile(
        r"^(?:git(?:\+[a-z0-9]+)?|https?|ssh|file|link|workspace|npm|github|gitlab|bitbucket):"
    )
    github_shorthand = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+(?:#.*)?$", re.IGNORECASE)
    if (
        not normalized
        or normalized.startswith((".", "/"))
        or "://" in lowered
        or non_registry_prefix.match(lowered)
        or github_shorthand.fullmatch(normalized)
    ):
        raise PackBuildError(f"拒绝非公开 npm registry 依赖：{name}@{spec}")


def source_dependencies(package_json: dict[str, Any]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for field in ("dependencies", "optionalDependencies"):
        values = package_json.get(field) or {}
        if not isinstance(values, dict):
            raise PackBuildError(f"package.json 的 {field} 不是对象")
        for name, spec in values.items():
            if not isinstance(name, str) or not isinstance(spec, str):
                continue
            normalized_name = name.strip()
            normalized_spec = spec.strip()
            if not normalized_name or not normalized_spec:
                continue
            _validate_registry_dependency_spec(normalized_name, normalized_spec)
            merged[normalized_name] = normalized_spec
    return dict(sorted(merged.items()))


def android_core_dependencies(package_json: dict[str, Any]) -> dict[str, str]:
    return {
        name: spec
        for name, spec in source_dependencies(package_json).items()
        if name not in EXCLUDED_CORE_DEPENDENCIES
    }


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def version_satisfies(spec: str, installed: str) -> bool:
    version = _parse_version(installed)
    if version is None:
        return False
    normalized = spec.strip()
    if normalized in {"*", "latest"}:
        return True
    if "||" in normalized:
        return any(version_satisfies(part, installed) for part in normalized.split("||"))
    exact = _parse_version(normalized)
    if exact is not None:
        return version == exact
    if normalized.startswith("^"):
        minimum = _parse_version(normalized[1:])
        if minimum is None:
            return False
        if minimum[0] > 0:
            maximum = (minimum[0] + 1, 0, 0)
        elif minimum[1] > 0:
            maximum = (0, minimum[1] + 1, 0)
        else:
            maximum = (0, 0, minimum[2] + 1)
        return minimum <= version < maximum
    if normalized.startswith("~"):
        minimum = _parse_version(normalized.lstrip("~>="))
        return minimum is not None and minimum <= version < (minimum[0], minimum[1] + 1, 0)
    if normalized.startswith(">="):
        parts = normalized.split()
        minimum = _parse_version(parts[0][2:])
        if minimum is None or version < minimum:
            return False
        if len(parts) == 1:
            return True
        if len(parts) == 2 and parts[1].startswith("<"):
            maximum = _parse_version(parts[1].lstrip("<="))
            return maximum is not None and version < maximum
    return False


def validate_core_coverage(
    core_package_json: dict[str, Any],
    runtime_package_json: dict[str, Any],
    label: str,
) -> None:
    runtime = source_dependencies(runtime_package_json)
    required = android_core_dependencies(core_package_json)
    uncovered: list[str] = []
    for name, spec in required.items():
        runtime_spec = runtime.get(name)
        installed = _parse_version(runtime_spec or "")
        if runtime_spec is None or installed is None or not version_satisfies(
            spec, ".".join(str(part) for part in installed)
        ):
            uncovered.append(f"{name}@{spec}")
    if uncovered:
        raise PackBuildError(
            f"公共运行时未覆盖{label}核心依赖：{', '.join(sorted(uncovered))}"
        )


def _package_roots(node_modules_dir: Path) -> Iterable[tuple[Path, str]]:
    if not node_modules_dir.is_dir():
        raise PackBuildError(f"缺少 node_modules：{node_modules_dir}")
    for package_json in sorted(node_modules_dir.rglob("package.json")):
        relative = package_json.relative_to(node_modules_dir)
        parts = relative.parts
        if len(parts) < 2:
            continue
        markers = [index for index, part in enumerate(parts[:-1]) if part == "node_modules"]
        start = markers[-1] + 1 if markers else 0
        package_parts = parts[start:-1]
        if package_parts and package_parts[0].startswith("@"):
            if len(package_parts) != 2:
                continue
            package_name = "/".join(package_parts)
        elif len(package_parts) == 1:
            package_name = package_parts[0]
        else:
            continue
        yield package_json.parent, package_name


def _iter_runtime_files(node_modules_dir: Path) -> Iterable[tuple[Path, str]]:
    for path in sorted(node_modules_dir.rglob("*")):
        relative = path.relative_to(node_modules_dir)
        if path.is_symlink():
            if relative.parts and relative.parts[0] == ".bin":
                continue
            raise PackBuildError(f"依赖包中禁止符号链接：{path}")
        if not path.is_file():
            continue
        relative_path = relative.as_posix()
        if relative_path == ".package-lock.json" or "/.package-lock.json" in relative_path:
            continue
        yield path, f"node_modules/{relative_path}"


def validate_package_tree(node_modules_dir: Path) -> None:
    package_count = 0
    for package_root, package_name in _package_roots(node_modules_dir):
        package_count += 1
        package_json = read_json(package_root / "package.json")
        if not isinstance(package_json, dict):
            raise PackBuildError(f"包清单不是对象：{package_root}")
        scripts = package_json.get("scripts") or {}
        if not isinstance(scripts, dict):
            raise PackBuildError(f"包 scripts 不是对象：{package_root}")
        bad_scripts = sorted(set(scripts).intersection(_DISALLOWED_INSTALL_SCRIPTS))
        if bad_scripts:
            raise PackBuildError(
                f"拒绝包含安装脚本的包：{package_name}（{', '.join(bad_scripts)}）"
            )
        if package_json.get("os") or package_json.get("cpu") or package_json.get("libc"):
            raise PackBuildError(f"拒绝带平台限定的包：{package_name}")
    for file_path, _ in _iter_runtime_files(node_modules_dir):
        if file_path.suffix.lower() in _NATIVE_SUFFIXES or file_path.name in _NATIVE_FILENAMES:
            raise PackBuildError(f"拒绝包含原生构建文件：{file_path}")
        if "prebuilds" in file_path.parts:
            raise PackBuildError(f"拒绝包含 prebuilds：{file_path}")
    if package_count == 0:
        raise PackBuildError("依赖树为空")


def _lock_package_map(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise PackBuildError("只支持 npm lockfileVersion 2/3 的 packages 字段")
    return {str(key): value for key, value in packages.items() if isinstance(value, dict)}


def validate_lockfile(lock: dict[str, Any], dependencies: dict[str, str]) -> None:
    packages = _lock_package_map(lock)
    root_dependencies = packages.get("", {}).get("dependencies") or {}
    if root_dependencies != dependencies:
        raise PackBuildError("runtime/package-lock.json 与 package.json 依赖不一致")
    for key, entry in packages.items():
        if entry.get("hasInstallScript"):
            raise PackBuildError(f"lockfile 标记了安装脚本：{key}")


def collect_package_records(node_modules_dir: Path, lock: dict[str, Any]) -> list[dict[str, Any]]:
    lock_packages = _lock_package_map(lock)
    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for package_root, package_name in _package_roots(node_modules_dir):
        if package_root in seen:
            continue
        seen.add(package_root)
        package_json = read_json(package_root / "package.json")
        relative_root = package_root.relative_to(node_modules_dir).as_posix()
        lock_entry = lock_packages.get(f"node_modules/{relative_root}", {})
        version = str(package_json.get("version") or "").strip()
        if not version:
            raise PackBuildError(f"包缺少版本：{package_root}")
        records.append(
            {
                "name": str(package_json.get("name") or package_name),
                "version": version,
                "integrity": lock_entry.get("integrity"),
                "path": f"node_modules/{relative_root}",
            }
        )
    return sorted(records, key=lambda item: (item["path"], item["name"]))


def _copy_core_for_smoke(core_dir: Path, target: Path) -> Path:
    source_subdir = core_dir / "danmu_api"
    if not source_subdir.is_dir():
        raise PackBuildError(f"核心缺少 danmu_api 目录：{source_subdir}")
    target.mkdir(parents=True, exist_ok=True)
    for source in source_subdir.iterdir():
        destination = target / source.name
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=False)
        else:
            shutil.copy2(source, destination)
    shutil.copy2(core_dir / "package.json", target / "package.json")
    return target


def run_worker_smoke(core_dir: Path, node_modules_dir: Path, label: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"danmu-pack-smoke-{label}-") as tmp:
        smoke_core = _copy_core_for_smoke(core_dir, Path(tmp) / "core")
        shutil.copytree(node_modules_dir, smoke_core / "node_modules", symlinks=False)
        result = subprocess.run(
            ["node", "--input-type=module", "-e", "import('./worker.js').then(() => process.exit(0))"],
            cwd=smoke_core,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise PackBuildError(f"{label}核心 worker.js smoke 失败：{detail}")


def _zip_deterministic(source_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [path for path in source_root.rglob("*") if path.is_file()]
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files, key=lambda item: item.relative_to(source_root).as_posix()):
            if path.is_symlink():
                raise PackBuildError(f"依赖包中禁止符号链接：{path}")
            relative = path.relative_to(source_root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def build_manifest(
    *,
    serial: int,
    node_major: int,
    runtime_lock: Path,
    dependencies: dict[str, str],
    archive: Path,
    package_records: list[dict[str, Any]],
    repository: str = PACK_REPO,
) -> dict[str, Any]:
    if serial <= 0:
        raise PackBuildError("manifest serial 必须大于 0")
    archive_sha256 = sha256_file(archive)
    tag = f"runtime-dependencies-{archive_sha256[:12]}"
    return {
        "schema": MANIFEST_SCHEMA,
        "serial": serial,
        "runtimeProtocol": RUNTIME_PROTOCOL,
        "nodeMajor": node_major,
        "runtimeLockSha256": sha256_file(runtime_lock),
        "dependencyFingerprint": dependency_fingerprint(dependencies),
        "dependencies": dependencies,
        "artifactUrl": (
            f"https://github.com/{repository}/releases/download/{tag}/node_modules.zip"
        ),
        "artifactSha256": archive_sha256,
        "artifactSize": archive.stat().st_size,
        "packages": package_records,
    }


def validate_runtime_definition(
    runtime_dir: Path,
    core_dirs: dict[str, Path],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    if set(core_dirs) != set(TRUSTED_CORE_LABELS):
        raise PackBuildError("必须同时校验 stable 与 dev 核心")
    runtime_package = read_json(runtime_dir / "package.json")
    runtime_lock = read_json(runtime_dir / "package-lock.json")
    if not isinstance(runtime_package, dict) or not isinstance(runtime_lock, dict):
        raise PackBuildError("runtime package 或 lock 格式无效")
    dependencies = source_dependencies(runtime_package)
    if not dependencies:
        raise PackBuildError("公共运行时依赖为空")
    for name, spec in dependencies.items():
        if _parse_version(spec) is None:
            raise PackBuildError(f"公共运行时必须锁定精确版本：{name}@{spec}")
    validate_lockfile(runtime_lock, dependencies)
    for label, core_dir in core_dirs.items():
        core_package = read_json(core_dir / "package.json")
        if not isinstance(core_package, dict):
            raise PackBuildError(f"{label}核心 package.json 格式无效")
        validate_core_coverage(core_package, runtime_package, label)
    return runtime_package, runtime_lock, dependencies


def build_pack(
    *,
    runtime_dir: Path,
    core_dirs: dict[str, Path],
    output_dir: Path,
    serial: int,
    repository: str = PACK_REPO,
    node_major: int = EMBEDDED_NODE_MAJOR,
    skip_smoke: bool = False,
) -> dict[str, Any]:
    if serial <= 0:
        raise PackBuildError("manifest serial 必须大于 0")
    _, runtime_lock, dependencies = validate_runtime_definition(runtime_dir, core_dirs)

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / "node_modules.zip"
    with tempfile.TemporaryDirectory(prefix="danmu-runtime-pack-") as tmp:
        work = Path(tmp)
        npm_project = work / "runtime"
        npm_project.mkdir()
        shutil.copy2(runtime_dir / "package.json", npm_project / "package.json")
        shutil.copy2(runtime_dir / "package-lock.json", npm_project / "package-lock.json")
        result = subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund"],
            cwd=npm_project,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-4000:]
            raise PackBuildError(f"npm ci 失败：{detail}")

        node_modules = npm_project / "node_modules"
        validate_package_tree(node_modules)
        package_records = collect_package_records(node_modules, runtime_lock)
        if not skip_smoke:
            for label, core_dir in core_dirs.items():
                run_worker_smoke(core_dir, node_modules, label)

        pack_root = work / "pack"
        shutil.copytree(
            node_modules,
            pack_root / "node_modules",
            symlinks=False,
            ignore=shutil.ignore_patterns(".bin", ".package-lock.json"),
        )
        _zip_deterministic(pack_root, archive_path)

    manifest = build_manifest(
        serial=serial,
        node_major=node_major,
        runtime_lock=runtime_dir / "package-lock.json",
        dependencies=dependencies,
        archive=archive_path,
        package_records=package_records,
        repository=repository,
    )
    write_canonical_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--stable-core-dir", type=Path, required=True)
    parser.add_argument("--dev-core-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--serial", type=int, default=0)
    parser.add_argument("--repository", default=PACK_REPO)
    parser.add_argument("--node-major", type=int, default=EMBEDDED_NODE_MAJOR)
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    core_dirs = {"stable": args.stable_core_dir, "dev": args.dev_core_dir}
    try:
        if args.validate_only:
            validate_runtime_definition(args.runtime_dir, core_dirs)
            print("stable/dev 核心依赖均已被公共运行时覆盖")
            return 0
        manifest = build_pack(
            runtime_dir=args.runtime_dir,
            core_dirs=core_dirs,
            output_dir=args.output_dir,
            serial=args.serial,
            repository=args.repository,
            node_major=args.node_major,
            skip_smoke=args.skip_smoke,
        )
    except PackBuildError as exc:
        parser.error(str(exc))
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
