import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_runtime_pack import (
    EMBEDDED_NODE_MAJOR,
    INDEX_SCHEMA,
    TRUSTED_CHANNELS,
    PackBuildError,
    UPSTREAM_CORE_REPO,
    _zip_deterministic,
    build_manifest,
    canonical_json_bytes,
    collect_package_records,
    dependency_fingerprint,
    filter_android_dependencies,
    merge_index_entry,
    new_channel_index,
    sha256_file,
    source_dependencies,
    trusted_channel,
    validate_channel_source,
    validate_package_tree,
)
from scripts.build_legacy_compat_pack import build_legacy_compat_pack
from scripts.update_legacy_index import update_legacy_index


class BuildRuntimePackTest(unittest.TestCase):
    def test_uses_only_the_stable_and_dev_core_repositories(self):
        self.assertEqual(UPSTREAM_CORE_REPO, "huangxd-/danmu_api")
        self.assertEqual(
            TRUSTED_CHANNELS,
            {
                "stable": {
                    "repo": "huangxd-/danmu_api",
                    "branch": "main",
                    "url": "https://github.com/huangxd-/danmu_api.git",
                },
                "dev": {
                    "repo": "lilixu3/danmu_api",
                    "branch": "main",
                    "url": "https://github.com/lilixu3/danmu_api.git",
                },
            },
        )
        self.assertEqual("lilixu3/danmu_api", trusted_channel("DEV")["repo"])
        with self.assertRaises(PackBuildError):
            trusted_channel("custom")
        with self.assertRaises(PackBuildError):
            validate_channel_source("dev", "huangxd-/danmu_api", "main")
        with self.assertRaises(PackBuildError):
            validate_channel_source("stable", "huangxd-/danmu_api", "test")

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

    def test_merge_index_rejects_cross_channel_entry(self):
        sha = "a" * 40
        fingerprint = "b" * 64
        dev_entry = {
            "channel": "dev",
            "coreRepo": "lilixu3/danmu_api",
            "coreBranch": "main",
            "coreSha": sha,
            "dependencyFingerprint": fingerprint,
        }
        with self.assertRaises(PackBuildError):
            merge_index_entry(
                new_channel_index("stable"),
                dev_entry,
                channel="stable",
            )

    def test_same_fingerprint_stays_separate_between_channels(self):
        sha = "a" * 40
        fingerprint = "b" * 64
        stable_entry = {
            "channel": "stable",
            "coreRepo": "huangxd-/danmu_api",
            "coreBranch": "main",
            "coreSha": sha,
            "dependencyFingerprint": fingerprint,
        }
        dev_entry = {
            "channel": "dev",
            "coreRepo": "lilixu3/danmu_api",
            "coreBranch": "main",
            "coreSha": sha,
            "dependencyFingerprint": fingerprint,
        }
        stable = merge_index_entry(
            new_channel_index("stable"), stable_entry, channel="stable"
        )
        dev = merge_index_entry(new_channel_index("dev"), dev_entry, channel="dev")
        self.assertEqual(stable_entry, stable["entries"][sha])
        self.assertEqual(dev_entry, dev["entries"][sha])
        self.assertEqual("stable", stable["channel"])
        self.assertEqual("dev", dev["channel"])

    def test_merge_index_replaces_existing_entry_only_when_explicitly_forced(self):
        sha = "a" * 40
        fingerprint = "b" * 64
        old = {
            "channel": "stable",
            "coreRepo": UPSTREAM_CORE_REPO,
            "coreBranch": "main",
            "coreSha": sha,
            "dependencyFingerprint": fingerprint,
            "artifactSha256": "1" * 64,
        }
        new = {**old, "artifactSha256": "2" * 64}
        index = merge_index_entry(
            new_channel_index("stable"), old, channel="stable"
        )
        with self.assertRaises(PackBuildError):
            merge_index_entry(index, new, channel="stable")
        result = merge_index_entry(index, new, channel="stable", replace=True)
        self.assertEqual(new, result["entries"][sha])
        self.assertEqual(sha, result["dependencyEntries"][fingerprint])

    def test_merge_index_rejects_entry_without_dependency_fingerprint(self):
        with self.assertRaises(PackBuildError):
            merge_index_entry(
                new_channel_index("stable"),
                {
                    "channel": "stable",
                    "coreRepo": UPSTREAM_CORE_REPO,
                    "coreBranch": "main",
                    "coreSha": "a" * 40,
                },
                channel="stable",
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
                channel="stable",
                core_repo=UPSTREAM_CORE_REPO,
                core_branch="main",
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
        self.assertEqual(INDEX_SCHEMA, manifest["schema"])
        self.assertEqual("stable", manifest["channel"])
        self.assertEqual(UPSTREAM_CORE_REPO, manifest["coreRepo"])
        self.assertEqual("main", manifest["coreBranch"])
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

    def test_builds_distinct_schema_one_legacy_pack_with_recomputed_hashes(self):
        sha = "a" * 40
        fingerprint = "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel_root = root / "channel"
            package_file = channel_root / "node_modules/pure-package/index.js"
            package_file.parent.mkdir(parents=True)
            package_payload = b"export const ready = true;\n"
            package_file.write_bytes(package_payload)
            (channel_root / "runtime-lock.json").write_text("{}", encoding="utf-8")
            manifest = {
                "schema": 2,
                "channel": "stable",
                "coreRepo": UPSTREAM_CORE_REPO,
                "coreBranch": "main",
                "coreSha": sha,
                "coreVersion": "1.0.0",
                "runtimeProtocol": 1,
                "nodeMajor": 18,
                "dependencyFingerprint": fingerprint,
                "packages": [],
                "files": [
                    {
                        "path": "node_modules/pure-package/index.js",
                        "size": len(package_payload),
                        "sha256": hashlib.sha256(package_payload).hexdigest(),
                    }
                ],
            }
            manifest_bytes = canonical_json_bytes(manifest)
            (channel_root / "manifest.json").write_bytes(manifest_bytes)
            channel_archive = root / f"runtime-pack-stable-{sha[:12]}.zip"
            _zip_deterministic(channel_root, channel_archive)
            channel_entry = {
                "channel": "stable",
                "coreRepo": UPSTREAM_CORE_REPO,
                "coreBranch": "main",
                "coreSha": sha,
                "coreVersion": "1.0.0",
                "runtimeProtocol": 1,
                "dependencyFingerprint": fingerprint,
                "artifactUrl": "https://example.invalid/stable.zip",
                "artifactSha256": sha256_file(channel_archive),
                "artifactSize": channel_archive.stat().st_size,
                "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "packages": [],
            }
            entry_path = root / "entry.json"
            entry_path.write_text(json.dumps(channel_entry), encoding="utf-8")
            output = root / "dist"

            legacy_entry = build_legacy_compat_pack(
                channel_entry_path=entry_path,
                channel_archive_path=channel_archive,
                output_dir=output,
            )
            legacy_archive = output / f"runtime-pack-{sha[:12]}.zip"
            with zipfile.ZipFile(legacy_archive) as archive:
                legacy_manifest_bytes = archive.read("manifest.json")
                legacy_manifest = json.loads(legacy_manifest_bytes)
            with zipfile.ZipFile(channel_archive) as archive:
                self.assertEqual(2, json.loads(archive.read("manifest.json"))["schema"])

            self.assertEqual(1, legacy_manifest["schema"])
            self.assertNotIn("channel", legacy_manifest)
            self.assertNotIn("coreBranch", legacy_manifest)
            self.assertNotEqual(channel_entry["artifactSha256"], legacy_entry["artifactSha256"])
            self.assertEqual(sha256_file(legacy_archive), legacy_entry["artifactSha256"])
            self.assertEqual(legacy_archive.stat().st_size, legacy_entry["artifactSize"])
            self.assertEqual(
                hashlib.sha256(legacy_manifest_bytes).hexdigest(),
                legacy_entry["manifestSha256"],
            )
            self.assertEqual(
                "https://github.com/lilixu3/danmu-api-runtime-packs/"
                f"releases/download/core-{sha[:12]}/runtime-pack-{sha[:12]}.zip",
                legacy_entry["artifactUrl"],
            )
            legacy_index = update_legacy_index(
                root / "index.json", output / "legacy-entry.json"
            )
            self.assertEqual(legacy_entry["artifactSha256"], legacy_index["entries"][sha]["artifactSha256"])

    def test_legacy_index_rejects_channel_zip_and_dev_entry(self):
        sha = "a" * 40
        fingerprint = "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stable_entry_path = root / "stable-entry.json"
            stable_entry_path.write_text(
                json.dumps(
                    {
                        "channel": "stable",
                        "coreRepo": UPSTREAM_CORE_REPO,
                        "coreBranch": "main",
                        "coreSha": sha,
                        "dependencyFingerprint": fingerprint,
                        "artifactUrl": "https://example.invalid/stable.zip",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(PackBuildError):
                update_legacy_index(root / "index.json", stable_entry_path)

            dev_entry_path = root / "dev-entry.json"
            dev_entry_path.write_text(
                json.dumps(
                    {
                        "channel": "dev",
                        "coreRepo": "lilixu3/danmu_api",
                        "coreBranch": "main",
                        "coreSha": sha,
                        "dependencyFingerprint": fingerprint,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(PackBuildError):
                update_legacy_index(root / "index.json", dev_entry_path)


if __name__ == "__main__":
    unittest.main()
