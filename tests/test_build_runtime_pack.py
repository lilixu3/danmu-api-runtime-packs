import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_runtime_pack import (
    EMBEDDED_NODE_MAJOR,
    PackBuildError,
    UPSTREAM_CORE_REPO,
    build_manifest,
    canonical_json_bytes,
    collect_package_records,
    dependency_fingerprint,
    filter_android_dependencies,
    merge_index_entry,
    source_dependencies,
    validate_package_tree,
)


class BuildRuntimePackTest(unittest.TestCase):
    def test_uses_the_official_upstream_core_repository(self):
        self.assertEqual(UPSTREAM_CORE_REPO, "huangxd-/danmu_api")

    def test_filters_only_explicit_non_android_dependencies(self):
        package_json = {
            "dependencies": {
                "brotli": "^1.3.3",
                "chokidar": "^4.0.3",
                "dotenv": "^16.4.7",
                "esbuild": "^0.25.10",
                "redis": "^5.11.0",
            }
        }
        policy = {
            "excludedDirectDependencies": {
                "chokidar": "server-only",
                "dotenv": "server-only",
                "esbuild": "build-only",
                "redis": "optional",
            }
        }
        self.assertEqual(
            {"brotli": "^1.3.3"},
            filter_android_dependencies(package_json, policy),
        )

    def test_rejects_non_registry_dependency_specs(self):
        bad_specs = (
            "git+https://github.com/example/pkg.git",
            "https://example.com/pkg.tgz",
            "file:../pkg",
            "workspace:*",
            "../local-package",
        )
        for spec in bad_specs:
            with self.subTest(spec=spec), self.assertRaises(PackBuildError):
                source_dependencies({"dependencies": {"unsafe-package": spec}})
        self.assertEqual(
            {"safe-package": "^1.2.3 || ~2.0.0"},
            source_dependencies({"dependencies": {"safe-package": "^1.2.3 || ~2.0.0"}}),
        )

    def test_rejects_libc_limited_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node_modules" / "platform-package"
            root.mkdir(parents=True)
            (root / "package.json").write_text(
                json.dumps(
                    {"name": "platform-package", "version": "1.0.0", "libc": ["glibc"]}
                ),
                encoding="utf-8",
            )
            with self.assertRaises(PackBuildError):
                validate_package_tree(Path(tmp) / "node_modules")

    def test_dependency_fingerprint_is_order_independent(self):
        left = {"brotli": "^1.3.3", "pako": "^2.1.0"}
        right = {"pako": "^2.1.0", "brotli": "^1.3.3"}
        self.assertEqual(dependency_fingerprint(left), dependency_fingerprint(right))
        self.assertEqual(
            "546a071745a850d49ec26f4b27dc7591d018e75e3d6cc45ede7d3cb9c604b0ff",
            dependency_fingerprint(left),
        )
        self.assertEqual(
            64,
            len(dependency_fingerprint(left)),
        )

    def test_rejects_lifecycle_install_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node_modules" / "bad-package"
            root.mkdir(parents=True)
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "bad-package",
                        "version": "1.0.0",
                        "scripts": {"install": "node install.js"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(PackBuildError):
                validate_package_tree(Path(tmp) / "node_modules")

    def test_rejects_native_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node_modules" / "native-package"
            root.mkdir(parents=True)
            (root / "package.json").write_text(
                json.dumps({"name": "native-package", "version": "1.0.0"}),
                encoding="utf-8",
            )
            (root / "binding.node").write_bytes(b"not-for-android")
            with self.assertRaises(PackBuildError):
                validate_package_tree(Path(tmp) / "node_modules")

    def test_collects_direct_and_transitive_package_records(self):
        lock = {
            "packages": {
                "": {"dependencies": {"brotli": "^1.3.3"}},
                "node_modules/brotli": {
                    "version": "1.3.3",
                    "resolved": "https://registry.npmjs.org/brotli/-/brotli-1.3.3.tgz",
                    "integrity": "sha512-brotli",
                    "dependencies": {"base64-js": "^1.1.2"},
                },
                "node_modules/base64-js": {
                    "version": "1.5.1",
                    "resolved": "https://registry.npmjs.org/base64-js/-/base64-js-1.5.1.tgz",
                    "integrity": "sha512-base64",
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            nm = Path(tmp) / "node_modules"
            for name, version in (("brotli", "1.3.3"), ("base64-js", "1.5.1")):
                pkg = nm / name
                pkg.mkdir(parents=True)
                (pkg / "package.json").write_text(
                    json.dumps({"name": name, "version": version}), encoding="utf-8"
                )
            records = collect_package_records(nm, lock)
        self.assertEqual(
            ["base64-js", "brotli"],
            [record["name"] for record in records],
        )
        self.assertEqual("sha512-brotli", records[1]["integrity"])

    def test_merge_index_rejects_non_upstream_entry(self):
        with self.assertRaises(PackBuildError):
            merge_index_entry(
                {"schema": 1, "entries": {}},
                {"coreRepo": "lilixu3/danmu_api", "coreSha": "abc"},
            )

    def test_merge_index_replaces_existing_entry_only_when_explicitly_forced(self):
        sha = "a" * 40
        fingerprint = "b" * 64
        old = {
            "coreRepo": UPSTREAM_CORE_REPO,
            "coreSha": sha,
            "dependencyFingerprint": fingerprint,
            "artifactSha256": "1" * 64,
        }
        new = {
            "coreRepo": UPSTREAM_CORE_REPO,
            "coreSha": sha,
            "dependencyFingerprint": fingerprint,
            "artifactSha256": "2" * 64,
        }
        index = {
            "schema": 1,
            "upstream": {"repo": UPSTREAM_CORE_REPO, "branch": "main"},
            "entries": {sha: old},
        }
        with self.assertRaises(PackBuildError):
            merge_index_entry(index, new)
        result = merge_index_entry(index, new, replace=True)
        self.assertEqual(new, result["entries"][sha])
        self.assertEqual(sha, result["dependencyEntries"][fingerprint])

    def test_merge_index_rejects_entry_without_dependency_fingerprint(self):
        with self.assertRaises(PackBuildError):
            merge_index_entry(
                {"schema": 1, "entries": {}},
                {"coreRepo": UPSTREAM_CORE_REPO, "coreSha": "a" * 40},
            )

    def test_ignores_package_internal_subpath_package_json(self):
        lock = {
            "packages": {
                "": {"dependencies": {"web-streams-polyfill": "3.3.3"}},
                "node_modules/web-streams-polyfill": {
                    "version": "3.3.3",
                    "integrity": "sha512-streams",
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node_modules" / "web-streams-polyfill"
            internal = root / "es2018"
            internal.mkdir(parents=True)
            (root / "package.json").write_text(
                json.dumps({"name": "web-streams-polyfill", "version": "3.3.3"}),
                encoding="utf-8",
            )
            (internal / "package.json").write_text(
                json.dumps({"type": "module"}), encoding="utf-8"
            )
            records = collect_package_records(Path(tmp) / "node_modules", lock)
        self.assertEqual(["web-streams-polyfill"], [record["name"] for record in records])

    def test_manifest_contains_hashes_for_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            nm = Path(tmp) / "node_modules" / "pure-package"
            nm.mkdir(parents=True)
            payload = b"export const ok = true;\n"
            (nm / "index.js").write_bytes(payload)
            (nm / "package.json").write_text(
                json.dumps({"name": "pure-package", "version": "1.0.0"}),
                encoding="utf-8",
            )
            manifest = build_manifest(
                core_repo=UPSTREAM_CORE_REPO,
                core_sha="a" * 40,
                core_version="1.19.16",
                dependency_fingerprint="b" * 64,
                node_modules_dir=Path(tmp) / "node_modules",
                package_records=[
                    {
                        "name": "pure-package",
                        "version": "1.0.0",
                        "integrity": None,
                    }
                ],
            )
        self.assertEqual(UPSTREAM_CORE_REPO, manifest["coreRepo"])
        self.assertEqual(18, EMBEDDED_NODE_MAJOR)
        self.assertEqual(EMBEDDED_NODE_MAJOR, manifest["nodeMajor"])
        paths = {item["path"]: item for item in manifest["files"]}
        self.assertIn("node_modules/pure-package/index.js", paths)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            paths["node_modules/pure-package/index.js"]["sha256"],
        )
        self.assertEqual(
            manifest,
            json.loads(canonical_json_bytes(manifest).decode("utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
