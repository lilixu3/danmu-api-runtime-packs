"""构建供 DanmuApiApp 使用的单一签名 node_modules 运行时依赖包。"""

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
EMBEDDED_NODE_VERSION = "18.20.4"
TRUSTED_CORE_LABELS = ("stable", "dev")
ANDROID_POLICY_FILE = "android-runtime-policy.json"
EXCLUDED_CORE_DEPENDENCIES = {"chokidar", "dotenv", "esbuild", "redis"}
_DISALLOWED_INSTALL_SCRIPTS = {"preinstall", "install", "postinstall"}
_NATIVE_SUFFIXES = {".node", ".so", ".dylib", ".dll"}
_NATIVE_FILENAMES = {"binding.gyp", "binding.cc", "binding.c", "binding.cpp"}
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$")
_CORE_VERSION = re.compile(r"^v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
_GLOBALS_VERSION = re.compile(r"\bVERSION\s*:\s*(['\"])([^'\"]+)\1")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_CORE_SOURCE_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}


class PackBuildError(RuntimeError):
    """公共纯 JavaScript 运行时无法安全发布。"""


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


def build_definition_sha256() -> str:
    digest = hashlib.sha256()
    script_paths = (Path(__file__), Path(__file__).with_name("android_runtime_smoke.mjs"))
    for path in script_paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def read_core_commit(core_dir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(core_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    commit = result.stdout.strip().lower()
    if result.returncode != 0 or not _COMMIT_SHA.fullmatch(commit):
        raise PackBuildError(f"无法识别核心提交：{core_dir}")
    return commit


def validate_node_executable(node_executable: str) -> None:
    result = subprocess.run(
        [node_executable, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    version = result.stdout.strip().removeprefix("v")
    if result.returncode != 0 or version != EMBEDDED_NODE_VERSION:
        raise PackBuildError(
            f"Android 运行时 smoke 必须使用 Node {EMBEDDED_NODE_VERSION}，实际为 "
            f"{version or '不可用'}（{node_executable}）"
        )


def load_android_policy(path: Path) -> dict[str, Any]:
    policy = read_json(path)
    if not isinstance(policy, dict) or policy.get("schema") != 1:
        raise PackBuildError(f"Android 运行时策略格式无效：{path}")
    required_maps = (
        "approvedPackages",
        "excludedPackages",
        "reviewedCoreImports",
        "retainedPackageFiles",
        "removedPackageFiles",
        "budgets",
    )
    for field in required_maps:
        if not isinstance(policy.get(field), dict):
            raise PackBuildError(f"Android 运行时策略缺少对象字段：{field}")
    for field in ("removedFileSuffixes", "removedDocumentPrefixes", "requiredFiles"):
        values = policy.get(field)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise PackBuildError(f"Android 运行时策略缺少字符串数组：{field}")
    return policy


def collect_core_package_references(core_dir: Path, package_name: str) -> set[str]:
    quoted_reference = re.compile(
        rf"(?P<quote>['\"`])(?P<specifier>{re.escape(package_name)}(?:/[^'\"`\s]*)?)(?P=quote)"
    )
    references: set[str] = set()
    for source in sorted(core_dir.rglob("*")):
        if (
            not source.is_file()
            or source.suffix.lower() not in _CORE_SOURCE_SUFFIXES
            or ".git" in source.parts
            or "node_modules" in source.parts
        ):
            continue
        try:
            content = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise PackBuildError(f"无法读取核心源码：{source}") from exc
        references.update(match.group("specifier") for match in quoted_reference.finditer(content))
    return references


def validate_reviewed_core_imports(
    core_dirs: dict[str, Path],
    policy: dict[str, Any],
) -> None:
    reviewed_imports = policy["reviewedCoreImports"]
    approved_paths = policy["approvedPackages"]
    all_references: dict[str, set[str]] = {
        package_name: set() for package_name in reviewed_imports
    }
    for package_name, imports in reviewed_imports.items():
        if not isinstance(package_name, str) or not package_name:
            raise PackBuildError("reviewedCoreImports 包含无效包名")
        if f"node_modules/{package_name}" not in approved_paths:
            raise PackBuildError(f"核心导入策略引用了未批准包：{package_name}")
        if (
            not isinstance(imports, list)
            or not imports
            or not all(
                isinstance(specifier, str)
                and (specifier == package_name or specifier.startswith(f"{package_name}/"))
                for specifier in imports
            )
            or len(set(imports)) != len(imports)
        ):
            raise PackBuildError(f"核心导入策略格式无效：{package_name}")

    for label, core_dir in core_dirs.items():
        for package_name, imports in reviewed_imports.items():
            references = collect_core_package_references(core_dir, package_name)
            unexpected = references.difference(imports)
            if unexpected:
                raise PackBuildError(
                    f"{label}核心使用了未经人工确认的 {package_name} 入口："
                    f"{', '.join(sorted(unexpected))}"
                )
            all_references[package_name].update(references)

    stale = {
        package_name: sorted(set(imports).difference(all_references[package_name]))
        for package_name, imports in reviewed_imports.items()
        if set(imports).difference(all_references[package_name])
    }
    if stale:
        details = "; ".join(
            f"{package_name}: {', '.join(imports)}"
            for package_name, imports in sorted(stale.items())
        )
        raise PackBuildError(f"核心已不再使用已确认入口，请人工评估继续精简：{details}")


def read_core_version(core_dir: Path) -> str:
    candidates = (
        core_dir / "danmu_api/configs/globals.js",
        core_dir / "danmu-api/configs/globals.js",
        core_dir / "configs/globals.js",
        core_dir / "config/globals.js",
        core_dir / "globals.js",
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise PackBuildError(f"无法读取核心版本：{candidate}") from exc
        match = _GLOBALS_VERSION.search(content)
        if match:
            version = match.group(2).strip()
            if not _CORE_VERSION.fullmatch(version):
                raise PackBuildError(f"核心版本格式无效：{version}")
            return version.removeprefix("v")

    package_json = read_json(core_dir / "package.json")
    version = str(package_json.get("version") or "").strip() if isinstance(package_json, dict) else ""
    if not _CORE_VERSION.fullmatch(version):
        raise PackBuildError(f"无法识别核心版本：{core_dir}")
    return version.removeprefix("v")


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
    shorthand = normalized.removeprefix("v").split(".")
    if 1 <= len(shorthand) <= 2 and all(part.isdigit() for part in shorthand):
        expected = tuple(int(part) for part in shorthand)
        return version[: len(expected)] == expected
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


def _validate_package_path(path: str) -> None:
    parts = path.split("/")
    if (
        len(parts) < 2
        or parts[0] != "node_modules"
        or any(not part or part in {".", ".."} for part in parts)
    ):
        raise PackBuildError(f"Android 运行时策略包含无效包路径：{path}")


def _policy_expected_versions(policy: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    approved: dict[str, str] = {}
    for path, version in policy["approvedPackages"].items():
        if not isinstance(path, str) or not isinstance(version, str) or not version.strip():
            raise PackBuildError("Android 运行时策略 approvedPackages 格式无效")
        _validate_package_path(path)
        approved[path] = version.strip()

    excluded: dict[str, str] = {}
    for path, config in policy["excludedPackages"].items():
        if not isinstance(path, str) or not isinstance(config, dict):
            raise PackBuildError("Android 运行时策略 excludedPackages 格式无效")
        _validate_package_path(path)
        version = config.get("version")
        if not isinstance(version, str) or not version.strip():
            raise PackBuildError(f"排除包缺少精确版本：{path}")
        excluded[path] = version.strip()

    overlap = sorted(set(approved).intersection(excluded))
    if overlap:
        raise PackBuildError(f"Android 策略同时允许和排除了包：{', '.join(overlap)}")
    return dict(sorted(approved.items())), dict(sorted(excluded.items()))


def _format_inventory_difference(
    actual: dict[str, str],
    expected: dict[str, str],
    label: str,
) -> str | None:
    added = sorted(set(actual).difference(expected))
    missing = sorted(set(expected).difference(actual))
    changed = sorted(
        f"{path} {expected[path]} -> {actual[path]}"
        for path in set(actual).intersection(expected)
        if actual[path] != expected[path]
    )
    if not added and not missing and not changed:
        return None
    details: list[str] = []
    if added:
        details.append(f"新增：{', '.join(added)}")
    if missing:
        details.append(f"缺少：{', '.join(missing)}")
    if changed:
        details.append(f"版本变化：{', '.join(changed)}")
    return f"{label}需要人工确认（{'；'.join(details)}）"


def validate_policy_lock_inventory(lock: dict[str, Any], policy: dict[str, Any]) -> None:
    approved, excluded = _policy_expected_versions(policy)
    expected = {**approved, **excluded}
    actual = {
        path: str(entry.get("version") or "").strip()
        for path, entry in _lock_package_map(lock).items()
        if path.startswith("node_modules/")
    }
    error = _format_inventory_difference(actual, expected, "npm 锁文件包集合")
    if error:
        raise PackBuildError(error)


def collect_package_inventory(node_modules_dir: Path) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for package_root, fallback_name in _package_roots(node_modules_dir):
        relative_root = package_root.relative_to(node_modules_dir).as_posix()
        path = f"node_modules/{relative_root}"
        package_json = read_json(package_root / "package.json")
        if not isinstance(package_json, dict):
            raise PackBuildError(f"包清单不是对象：{package_root}")
        version = str(package_json.get("version") or "").strip()
        name = str(package_json.get("name") or fallback_name).strip()
        if not name or not version:
            raise PackBuildError(f"包缺少名称或版本：{package_root}")
        if path in inventory:
            raise PackBuildError(f"检测到重复 npm 包路径：{path}")
        inventory[path] = {
            "name": name,
            "version": version,
            "root": package_root,
            "package": package_json,
        }
    return dict(sorted(inventory.items()))


def _inventory_versions(inventory: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {path: str(item["version"]) for path, item in inventory.items()}


def _tree_stats(node_modules_dir: Path, package_count: int) -> dict[str, int]:
    file_count = 0
    extracted_bytes = 0
    for file_path, _ in _iter_runtime_files(node_modules_dir):
        file_count += 1
        extracted_bytes += file_path.stat().st_size
    return {
        "packageCount": package_count,
        "fileCount": file_count,
        "extractedBytes": extracted_bytes,
    }


def _required_dependencies(package_json: dict[str, Any]) -> dict[str, str]:
    required: dict[str, str] = {}
    dependencies = package_json.get("dependencies") or {}
    if not isinstance(dependencies, dict):
        raise PackBuildError("npm 包 dependencies 不是对象")
    for name, spec in dependencies.items():
        if isinstance(name, str) and isinstance(spec, str) and name and spec:
            required[name] = spec

    peers = package_json.get("peerDependencies") or {}
    peer_meta = package_json.get("peerDependenciesMeta") or {}
    if not isinstance(peers, dict) or not isinstance(peer_meta, dict):
        raise PackBuildError("npm 包 peerDependencies 格式无效")
    for name, spec in peers.items():
        metadata = peer_meta.get(name) or {}
        if isinstance(metadata, dict) and metadata.get("optional") is True:
            continue
        if isinstance(name, str) and isinstance(spec, str) and name and spec:
            required[name] = spec
    return required


def _resolve_package_dependency(
    package_root: Path,
    dependency_name: str,
    node_modules_dir: Path,
) -> Path | None:
    candidates = [package_root / "node_modules" / dependency_name]
    for ancestor in package_root.parents:
        if ancestor.name == "node_modules":
            candidates.append(ancestor / dependency_name)
        if ancestor == node_modules_dir.parent:
            break
    seen: set[Path] = set()
    for candidate in candidates:
        normalized = candidate.resolve()
        if normalized in seen:
            continue
        seen.add(normalized)
        if (normalized / "package.json").is_file():
            return normalized
    return None


def validate_dependency_closure(node_modules_dir: Path) -> None:
    inventory = collect_package_inventory(node_modules_dir)
    for item in inventory.values():
        package_root = item["root"]
        package_json = item["package"]
        for dependency_name, version_range in _required_dependencies(package_json).items():
            resolved = _resolve_package_dependency(
                package_root,
                dependency_name,
                node_modules_dir,
            )
            if resolved is None:
                raise PackBuildError(
                    f"Android 依赖闭包不完整：{item['name']} 缺少 {dependency_name}@{version_range}"
                )
            installed = read_json(resolved / "package.json")
            installed_version = (
                str(installed.get("version") or "").strip()
                if isinstance(installed, dict)
                else ""
            )
            if not version_satisfies(version_range, installed_version):
                raise PackBuildError(
                    f"Android 依赖版本不匹配：{item['name']} 需要 "
                    f"{dependency_name}@{version_range}，实际为 {installed_version or '未知'}"
                )


def _matches_document_prefix(name: str, prefixes: list[str]) -> bool:
    lowered = name.lower()
    return any(
        lowered == prefix or lowered.startswith(f"{prefix}.") or lowered.startswith(f"{prefix}-")
        for prefix in prefixes
    )


def _validate_budget(name: str, actual: int, maximum: Any) -> None:
    if not isinstance(maximum, int) or maximum <= 0:
        raise PackBuildError(f"Android 运行时预算无效：{name}")
    if actual > maximum:
        raise PackBuildError(f"Android 运行时超过预算：{name}={actual}，上限={maximum}")


def apply_android_policy(
    node_modules_dir: Path,
    policy: dict[str, Any],
) -> dict[str, Any]:
    approved, excluded = _policy_expected_versions(policy)
    full_inventory = collect_package_inventory(node_modules_dir)
    expected_full = {**approved, **excluded}
    error = _format_inventory_difference(
        _inventory_versions(full_inventory),
        expected_full,
        "npm 安装包集合",
    )
    if error:
        raise PackBuildError(error)
    full_stats = _tree_stats(node_modules_dir, len(full_inventory))

    owner_documents: dict[str, dict[str, Any]] = {}
    moved_by_owner: dict[str, list[str]] = {}
    excluded_names: list[str] = []
    for package_path, config in sorted(policy["excludedPackages"].items()):
        package_item = full_inventory[package_path]
        package_name = str(package_item["name"])
        excluded_names.append(package_name)
        owner_path = config.get("ownerPath")
        owner_version = config.get("ownerVersion")
        if not isinstance(owner_path, str) or owner_path not in full_inventory:
            raise PackBuildError(f"排除规则缺少依赖所有者：{package_path}")
        owner_item = full_inventory[owner_path]
        if owner_item["version"] != owner_version:
            raise PackBuildError(
                f"排除规则所有者版本变化：{owner_path} "
                f"{owner_version} -> {owner_item['version']}"
            )
        owner_document = owner_documents.setdefault(owner_path, dict(owner_item["package"]))
        dependencies = owner_document.get("dependencies") or {}
        optional_dependencies = owner_document.get("optionalDependencies") or {}
        if not isinstance(dependencies, dict) or not isinstance(optional_dependencies, dict):
            raise PackBuildError(f"排除规则所有者依赖格式无效：{owner_path}")
        dependency_spec = dependencies.pop(package_name, None)
        if not isinstance(dependency_spec, str) or not dependency_spec:
            raise PackBuildError(
                f"排除规则不再匹配：{owner_path} 未声明 {package_name}"
            )
        optional_dependencies[package_name] = dependency_spec
        owner_document["dependencies"] = dependencies
        owner_document["optionalDependencies"] = optional_dependencies
        moved_by_owner.setdefault(owner_path, []).append(package_name)

    transformed_paths: set[str] = set()
    for owner_path, document in owner_documents.items():
        document["danmuApiAppRuntime"] = {
            "policySchema": policy["schema"],
            "movedToOptionalDependencies": sorted(moved_by_owner[owner_path]),
        }
        package_json_path = full_inventory[owner_path]["root"] / "package.json"
        package_json_path.write_bytes(canonical_json_bytes(document) + b"\n")
        transformed_paths.add(owner_path)

    for package_path in sorted(excluded):
        package_root = full_inventory[package_path]["root"]
        shutil.rmtree(package_root)

    for package_path, config in policy["retainedPackageFiles"].items():
        package_root = node_modules_dir / package_path.removeprefix("node_modules/")
        package_json = read_json(package_root / "package.json")
        actual_version = str(package_json.get("version") or "") if isinstance(package_json, dict) else ""
        if actual_version != config.get("version"):
            raise PackBuildError(
                f"文件白名单包版本变化：{package_path} {config.get('version')} -> {actual_version}"
            )
        retained = config.get("files")
        if not isinstance(retained, list) or not all(isinstance(path, str) for path in retained):
            raise PackBuildError(f"文件白名单格式无效：{package_path}")
        retained_set = set(retained)
        missing_retained = sorted(
            path for path in retained_set if not (package_root / path).is_file()
        )
        if missing_retained:
            raise PackBuildError(
                f"文件白名单需要重新确认：{package_path} 缺少 {', '.join(missing_retained)}"
            )
        for file_path in [path for path in package_root.rglob("*") if path.is_file()]:
            if file_path.relative_to(package_root).as_posix() not in retained_set:
                file_path.unlink()

    for package_path, config in policy["removedPackageFiles"].items():
        package_root = node_modules_dir / package_path.removeprefix("node_modules/")
        package_json = read_json(package_root / "package.json")
        actual_version = str(package_json.get("version") or "") if isinstance(package_json, dict) else ""
        if actual_version != config.get("version"):
            raise PackBuildError(
                f"文件排除包版本变化：{package_path} {config.get('version')} -> {actual_version}"
            )
        files = config.get("files")
        if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
            raise PackBuildError(f"文件排除规则格式无效：{package_path}")
        for relative_path in files:
            target = package_root / relative_path
            if not target.is_file():
                raise PackBuildError(
                    f"文件排除规则需要重新确认：{package_path}/{relative_path} 不存在"
                )
            target.unlink()

    suffixes = [suffix.lower() for suffix in policy["removedFileSuffixes"]]
    document_prefixes = [prefix.lower() for prefix in policy["removedDocumentPrefixes"]]
    for file_path, _ in list(_iter_runtime_files(node_modules_dir)):
        lowered_path = file_path.name.lower()
        if any(lowered_path.endswith(suffix) for suffix in suffixes) or _matches_document_prefix(
            file_path.name,
            document_prefixes,
        ):
            file_path.unlink()

    directories = sorted(
        (path for path in node_modules_dir.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        if not any(directory.iterdir()):
            directory.rmdir()

    validate_package_tree(node_modules_dir)
    validate_dependency_closure(node_modules_dir)
    android_inventory = collect_package_inventory(node_modules_dir)
    error = _format_inventory_difference(
        _inventory_versions(android_inventory),
        approved,
        "Android 发布包集合",
    )
    if error:
        raise PackBuildError(error)

    missing_required = sorted(
        relative_path
        for relative_path in policy["requiredFiles"]
        if not (node_modules_dir / relative_path).is_file()
    )
    if missing_required:
        raise PackBuildError(f"Android 运行时缺少关键文件：{', '.join(missing_required)}")

    android_stats = _tree_stats(node_modules_dir, len(android_inventory))
    budgets = policy["budgets"]
    _validate_budget("packageCount", android_stats["packageCount"], budgets.get("maxPackageCount"))
    _validate_budget("fileCount", android_stats["fileCount"], budgets.get("maxFileCount"))
    _validate_budget(
        "extractedBytes",
        android_stats["extractedBytes"],
        budgets.get("maxExtractedBytes"),
    )
    return {
        "full": full_stats,
        "android": android_stats,
        "excludedPackages": sorted(excluded_names),
        "transformedPackagePaths": transformed_paths,
    }


def collect_package_records(
    node_modules_dir: Path,
    lock: dict[str, Any],
    transformed_package_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    lock_packages = _lock_package_map(lock)
    transformed_package_paths = transformed_package_paths or set()
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
        package_path = f"node_modules/{relative_root}"
        record = {
            "name": str(package_json.get("name") or package_name),
            "version": version,
            "integrity": lock_entry.get("integrity"),
            "path": package_path,
        }
        if package_path in transformed_package_paths:
            record["sourceIntegrity"] = record["integrity"]
            record["integrity"] = None
            record["transformed"] = True
        records.append(record)
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


def run_worker_smoke(
    core_dir: Path,
    node_modules_dir: Path,
    label: str,
    node_executable: str = "node",
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"danmu-pack-smoke-{label}-") as tmp:
        smoke_core = _copy_core_for_smoke(core_dir, Path(tmp) / "core")
        shutil.copytree(node_modules_dir, smoke_core / "node_modules", symlinks=False)
        smoke_entry = smoke_core / ".danmu-runtime-worker-smoke.mjs"
        smoke_entry.write_text(
            "await import('./worker.js');\nprocess.exit(0);\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [node_executable, smoke_entry.name],
            cwd=smoke_core,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise PackBuildError(f"{label}核心 worker.js smoke 失败：{detail}")


def run_android_runtime_smoke(
    node_modules_dir: Path,
    node_executable: str = "node",
) -> None:
    smoke_source = Path(__file__).with_name("android_runtime_smoke.mjs")
    if not smoke_source.is_file():
        raise PackBuildError(f"缺少 Android 运行时 smoke：{smoke_source}")
    with tempfile.TemporaryDirectory(prefix="danmu-android-runtime-smoke-") as tmp:
        smoke_root = Path(tmp)
        shutil.copy2(smoke_source, smoke_root / smoke_source.name)
        smoke_node_modules = smoke_root / "node_modules"
        try:
            smoke_node_modules.symlink_to(node_modules_dir.resolve(), target_is_directory=True)
        except OSError:
            shutil.copytree(node_modules_dir, smoke_node_modules, symlinks=False)
        result = subprocess.run(
            [node_executable, smoke_source.name],
            cwd=smoke_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-3000:]
            raise PackBuildError(f"Android 精简运行时功能 smoke 失败：{detail}")


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
    runtime_policy: Path,
    dependencies: dict[str, str],
    core_versions: dict[str, str],
    core_commits: dict[str, str],
    archive: Path,
    package_records: list[dict[str, Any]],
    artifact_file_count: int,
    artifact_extracted_size: int,
    excluded_packages: list[str],
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
        "runtimePolicySha256": sha256_file(runtime_policy),
        "buildDefinitionSha256": build_definition_sha256(),
        "dependencyFingerprint": dependency_fingerprint(dependencies),
        "dependencies": dependencies,
        "coreVersions": core_versions,
        "coreCommits": core_commits,
        "artifactUrl": (
            f"https://github.com/{repository}/releases/download/{tag}/node_modules.zip"
        ),
        "artifactSha256": archive_sha256,
        "artifactSize": archive.stat().st_size,
        "artifactFileCount": artifact_file_count,
        "artifactExtractedSize": artifact_extracted_size,
        "excludedPackages": excluded_packages,
        "packages": package_records,
    }


def validate_runtime_definition(
    runtime_dir: Path,
    core_dirs: dict[str, Path],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, str]]:
    if set(core_dirs) != set(TRUSTED_CORE_LABELS):
        raise PackBuildError("必须同时校验 stable 与 dev 核心")
    runtime_package = read_json(runtime_dir / "package.json")
    runtime_lock = read_json(runtime_dir / "package-lock.json")
    runtime_policy = load_android_policy(runtime_dir / ANDROID_POLICY_FILE)
    if not isinstance(runtime_package, dict) or not isinstance(runtime_lock, dict):
        raise PackBuildError("runtime package 或 lock 格式无效")
    dependencies = source_dependencies(runtime_package)
    if not dependencies:
        raise PackBuildError("公共运行时依赖为空")
    for name, spec in dependencies.items():
        if _parse_version(spec) is None:
            raise PackBuildError(f"公共运行时必须锁定精确版本：{name}@{spec}")
    validate_lockfile(runtime_lock, dependencies)
    validate_policy_lock_inventory(runtime_lock, runtime_policy)
    validate_reviewed_core_imports(core_dirs, runtime_policy)
    core_versions: dict[str, str] = {}
    for label, core_dir in core_dirs.items():
        core_package = read_json(core_dir / "package.json")
        if not isinstance(core_package, dict):
            raise PackBuildError(f"{label}核心 package.json 格式无效")
        validate_core_coverage(core_package, runtime_package, label)
        core_versions[label] = read_core_version(core_dir)
    return runtime_package, runtime_lock, dependencies, core_versions


def build_pack(
    *,
    runtime_dir: Path,
    core_dirs: dict[str, Path],
    output_dir: Path,
    serial: int,
    repository: str = PACK_REPO,
    node_major: int = EMBEDDED_NODE_MAJOR,
    node_executable: str = "node",
    skip_smoke: bool = False,
) -> dict[str, Any]:
    if serial <= 0:
        raise PackBuildError("manifest serial 必须大于 0")
    if node_major != EMBEDDED_NODE_MAJOR:
        raise PackBuildError(f"只支持 Node 主版本 {EMBEDDED_NODE_MAJOR}")
    if not skip_smoke:
        validate_node_executable(node_executable)
    _, runtime_lock, dependencies, core_versions = validate_runtime_definition(
        runtime_dir,
        core_dirs,
    )
    runtime_policy_path = runtime_dir / ANDROID_POLICY_FILE
    runtime_policy = load_android_policy(runtime_policy_path)
    core_commits = {
        label: read_core_commit(core_dir)
        for label, core_dir in core_dirs.items()
    }

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
        pack_root = work / "pack"
        shutil.copytree(
            node_modules,
            pack_root / "node_modules",
            symlinks=False,
            ignore=shutil.ignore_patterns(".bin", ".package-lock.json"),
        )
        android_node_modules = pack_root / "node_modules"
        policy_report = apply_android_policy(android_node_modules, runtime_policy)
        package_records = collect_package_records(
            android_node_modules,
            runtime_lock,
            policy_report["transformedPackagePaths"],
        )
        if not skip_smoke:
            run_android_runtime_smoke(android_node_modules, node_executable)
            for label, core_dir in core_dirs.items():
                run_worker_smoke(core_dir, android_node_modules, label, node_executable)

        _zip_deterministic(pack_root, archive_path)

    archive_size = archive_path.stat().st_size
    _validate_budget(
        "archiveBytes",
        archive_size,
        runtime_policy["budgets"].get("maxArchiveBytes"),
    )
    android_stats = policy_report["android"]
    build_report = {
        "schema": 1,
        "runtimePolicySha256": sha256_file(runtime_policy_path),
        "buildDefinitionSha256": build_definition_sha256(),
        "coreVersions": core_versions,
        "coreCommits": core_commits,
        "full": policy_report["full"],
        "android": {
            **android_stats,
            "archiveBytes": archive_size,
        },
        "excludedPackages": policy_report["excludedPackages"],
    }
    write_canonical_json(output_dir / "build-report.json", build_report)

    manifest = build_manifest(
        serial=serial,
        node_major=node_major,
        runtime_lock=runtime_dir / "package-lock.json",
        runtime_policy=runtime_policy_path,
        dependencies=dependencies,
        core_versions=core_versions,
        core_commits=core_commits,
        archive=archive_path,
        package_records=package_records,
        artifact_file_count=android_stats["fileCount"],
        artifact_extracted_size=android_stats["extractedBytes"],
        excluded_packages=policy_report["excludedPackages"],
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
    parser.add_argument("--node-executable", default="node")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    core_dirs = {"stable": args.stable_core_dir, "dev": args.dev_core_dir}
    try:
        if args.validate_only:
            validate_runtime_definition(args.runtime_dir, core_dirs)
            print("稳定版和开发版核心依赖均已被公共运行时覆盖")
            return 0
        manifest = build_pack(
            runtime_dir=args.runtime_dir,
            core_dirs=core_dirs,
            output_dir=args.output_dir,
            serial=args.serial,
            repository=args.repository,
            node_major=args.node_major,
            node_executable=args.node_executable,
            skip_smoke=args.skip_smoke,
        )
    except PackBuildError as exc:
        parser.error(str(exc))
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
