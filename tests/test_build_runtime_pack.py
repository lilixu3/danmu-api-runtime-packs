from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_runtime_pack import (
    MANIFEST_SCHEMA,
    RUNTIME_PROTOCOL,
    PackBuildError,
    _zip_deterministic,
    apply_android_policy,
    build_manifest,
    canonical_json_bytes,
    collect_package_records,
    collect_core_package_references,
    dependency_fingerprint,
    read_core_version,
    source_dependencies,
    validate_core_coverage,
    validate_lockfile,
    validate_package_tree,
    validate_policy_lock_inventory,
    validate_reviewed_core_imports,
    version_satisfies,
)


class RuntimePackBuilderTest(unittest.TestCase):
    @staticmethod
    def runtime_package() -> dict:
        return {
            "dependencies": {
                "@dan-uni/dan-any": "2.3.9",
                "brotli": "1.3.3",
                "https-proxy-agent": "7.0.6",
                "node-fetch": "3.3.2",
                "opencc-js": "1.4.1",
                "pako": "2.1.0",
            }
        }

    @staticmethod
    def minimal_android_policy() -> dict:
        return {
            "schema": 1,
            "approvedPackages": {
                "node_modules/keep": "1.0.0",
                "node_modules/owner": "2.0.0",
            },
            "excludedPackages": {
                "node_modules/drop": {
                    "version": "3.0.0",
                    "ownerPath": "node_modules/owner",
                    "ownerVersion": "2.0.0",
                    "reason": "test-only optional feature",
                }
            },
            "reviewedCoreImports": {
                "owner": ["owner/runtime"],
            },
            "retainedPackageFiles": {},
            "removedPackageFiles": {},
            "removedFileSuffixes": [".map", ".d.ts"],
            "removedDocumentPrefixes": ["readme"],
            "requiredFiles": ["keep/index.js", "owner/index.js"],
            "budgets": {
                "maxArchiveBytes": 4096,
                "maxExtractedBytes": 4096,
                "maxFileCount": 10,
                "maxPackageCount": 2,
            },
        }

    def test_common_runtime_covers_stable_and_dev(self):
        stable = {
            "dependencies": {
                "chokidar": "^4.0.3",
                "dotenv": "^16.4.7",
                "esbuild": "^0.25.10",
                "https-proxy-agent": "^7.0.6",
                "node-fetch": "^3.3.2",
                "pako": "^2.1.0",
                "redis": "^5.11.0",
            }
        }
        dev = {
            "dependencies": {
                **stable["dependencies"],
                "@dan-uni/dan-any": "^2.3.9",
                "brotli": "^1.3.3",
                "opencc-js": "^1.4.1",
            }
        }
        validate_core_coverage(stable, self.runtime_package(), "stable")
        validate_core_coverage(dev, self.runtime_package(), "dev")

    def test_missing_dev_dependency_is_rejected(self):
        core = {"dependencies": {"opencc-js": "^1.4.1"}}
        runtime = self.runtime_package()
        del runtime["dependencies"]["opencc-js"]
        with self.assertRaisesRegex(PackBuildError, "opencc-js"):
            validate_core_coverage(core, runtime, "dev")

    def test_server_build_and_optional_dependencies_are_not_required(self):
        core = {
            "dependencies": {
                "chokidar": "^4.0.3",
                "dotenv": "^16.4.7",
                "esbuild": "^0.25.10",
                "redis": "^5.11.0",
            }
        }
        validate_core_coverage(core, self.runtime_package(), "stable")

    def test_semver_ranges_used_by_core_are_supported(self):
        self.assertTrue(version_satisfies("^2.3.9", "2.3.9"))
        self.assertTrue(version_satisfies("~1.4.1", "1.4.9"))
        self.assertTrue(version_satisfies(">=1.3.0 <2.0.0", "1.3.3"))
        self.assertTrue(version_satisfies("4", "4.4.3"))
        self.assertFalse(version_satisfies("4", "5.0.0"))
        self.assertFalse(version_satisfies("^2.3.9", "3.0.0"))
        self.assertFalse(version_satisfies("^0.2.3", "0.3.0"))

    def test_non_registry_dependency_is_rejected(self):
        with self.assertRaises(PackBuildError):
            source_dependencies({"dependencies": {"bad": "git+https://example.invalid/repo.git"}})

    def test_lockfile_must_match_runtime_roots_and_have_no_install_scripts(self):
        dependencies = {"brotli": "1.3.3"}
        validate_lockfile(
            {"packages": {"": {"dependencies": dependencies}, "node_modules/brotli": {}}},
            dependencies,
        )
        with self.assertRaisesRegex(PackBuildError, "安装脚本"):
            validate_lockfile(
                {
                    "packages": {
                        "": {"dependencies": dependencies},
                        "node_modules/brotli": {"hasInstallScript": True},
                    }
                },
                dependencies,
            )

    def test_policy_requires_manual_confirmation_for_new_lock_package(self):
        policy = self.minimal_android_policy()
        lock = {
            "packages": {
                "": {},
                "node_modules/keep": {"version": "1.0.0"},
                "node_modules/owner": {"version": "2.0.0"},
                "node_modules/drop": {"version": "3.0.0"},
                "node_modules/unreviewed": {"version": "1.0.0"},
            }
        }
        with self.assertRaisesRegex(PackBuildError, "需要人工确认.*unreviewed"):
            validate_policy_lock_inventory(lock, policy)

    def test_core_import_policy_requires_manual_confirmation_for_new_entrypoint(self):
        policy = self.minimal_android_policy()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stable = root / "stable"
            dev = root / "dev"
            stable.mkdir()
            dev.mkdir()
            (stable / "main.js").write_text(
                "import value from 'owner/runtime';\n",
                encoding="utf-8",
            )
            (dev / "main.js").write_text(
                "const value = await import(`owner/new-entry`);\n",
                encoding="utf-8",
            )
            self.assertEqual(
                {"owner/runtime"},
                collect_core_package_references(stable, "owner"),
            )
            with self.assertRaisesRegex(PackBuildError, "未经人工确认.*owner/new-entry"):
                validate_reviewed_core_imports({"stable": stable, "dev": dev}, policy)

    def test_core_import_policy_rejects_stale_reviewed_entrypoint(self):
        policy = self.minimal_android_policy()
        with tempfile.TemporaryDirectory() as tmp:
            stable = Path(tmp) / "stable"
            dev = Path(tmp) / "dev"
            stable.mkdir()
            dev.mkdir()
            with self.assertRaisesRegex(PackBuildError, "人工评估继续精简"):
                validate_reviewed_core_imports({"stable": stable, "dev": dev}, policy)

    def test_android_policy_prunes_and_preserves_local_import_closure(self):
        policy = self.minimal_android_policy()
        with tempfile.TemporaryDirectory() as tmp:
            node_modules = Path(tmp) / "node_modules"
            packages = {
                "keep": {
                    "name": "keep",
                    "version": "1.0.0",
                },
                "owner": {
                    "name": "owner",
                    "version": "2.0.0",
                    "dependencies": {
                        "drop": "^3.0.0",
                        "keep": "^1.0.0",
                    },
                },
                "drop": {
                    "name": "drop",
                    "version": "3.0.0",
                },
            }
            for name, package_json in packages.items():
                package = node_modules / name
                package.mkdir(parents=True)
                (package / "package.json").write_text(
                    json.dumps(package_json),
                    encoding="utf-8",
                )
                (package / "index.js").write_text("export {};\n", encoding="utf-8")
            (node_modules / "owner" / "index.js.map").write_text("{}", encoding="utf-8")
            (node_modules / "owner" / "README.md").write_text("docs", encoding="utf-8")

            report = apply_android_policy(node_modules, policy)

            self.assertFalse((node_modules / "drop").exists())
            owner = json.loads((node_modules / "owner" / "package.json").read_text())
            self.assertNotIn("drop", owner["dependencies"])
            self.assertEqual("^3.0.0", owner["optionalDependencies"]["drop"])
            self.assertEqual(
                ["drop"],
                owner["danmuApiAppRuntime"]["movedToOptionalDependencies"],
            )
            self.assertFalse((node_modules / "owner" / "index.js.map").exists())
            self.assertFalse((node_modules / "owner" / "README.md").exists())
            self.assertEqual(2, report["android"]["packageCount"])
            self.assertEqual({"node_modules/owner"}, report["transformedPackagePaths"])

    def test_android_policy_is_bound_to_owner_version(self):
        policy = self.minimal_android_policy()
        policy["approvedPackages"]["node_modules/owner"] = "2.1.0"
        with tempfile.TemporaryDirectory() as tmp:
            node_modules = Path(tmp) / "node_modules"
            for name, version in (("keep", "1.0.0"), ("owner", "2.1.0"), ("drop", "3.0.0")):
                package = node_modules / name
                package.mkdir(parents=True)
                dependencies = {"drop": "^3.0.0"} if name == "owner" else {}
                (package / "package.json").write_text(
                    json.dumps({"name": name, "version": version, "dependencies": dependencies}),
                    encoding="utf-8",
                )
                (package / "index.js").write_text("export {};\n", encoding="utf-8")
            with self.assertRaisesRegex(PackBuildError, "所有者版本变化"):
                apply_android_policy(node_modules, policy)

    def test_package_tree_rejects_install_scripts_and_native_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "node_modules" / "unsafe"
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "unsafe",
                        "version": "1.0.0",
                        "scripts": {"install": "node install.js"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PackBuildError, "安装脚本"):
                validate_package_tree(Path(tmp) / "node_modules")

            (package / "package.json").write_text(
                json.dumps({"name": "unsafe", "version": "1.0.0"}),
                encoding="utf-8",
            )
            (package / "binding.node").write_bytes(b"native")
            with self.assertRaisesRegex(PackBuildError, "原生"):
                validate_package_tree(Path(tmp) / "node_modules")

    def test_collects_direct_and_transitive_package_records(self):
        lock = {
            "packages": {
                "": {"dependencies": {"brotli": "1.3.3"}},
                "node_modules/brotli": {"version": "1.3.3", "integrity": "sha512-brotli"},
                "node_modules/base64-js": {"version": "1.5.1", "integrity": "sha512-base64"},
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            node_modules = Path(tmp) / "node_modules"
            for name, version in (("brotli", "1.3.3"), ("base64-js", "1.5.1")):
                package = node_modules / name
                package.mkdir(parents=True)
                (package / "package.json").write_text(
                    json.dumps({"name": name, "version": version}), encoding="utf-8"
                )
            records = collect_package_records(node_modules, lock)
        self.assertEqual(["base64-js", "brotli"], [record["name"] for record in records])
        self.assertEqual("sha512-brotli", records[1]["integrity"])

    def test_zip_contains_only_one_node_modules_root_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            package = source / "node_modules" / "pure-package"
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                '{"name":"pure-package","version":"1.0.0"}', encoding="utf-8"
            )
            first = root / "first.zip"
            second = root / "second.zip"
            _zip_deterministic(source, first)
            _zip_deterministic(source, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(
                    ["node_modules/pure-package/package.json"], archive.namelist()
                )

    def test_manifest_is_single_pack_metadata_without_channel_or_file_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "node_modules.zip"
            archive.write_bytes(b"zip fixture")
            lock = root / "package-lock.json"
            lock.write_bytes(b"lock fixture")
            policy = root / "android-runtime-policy.json"
            policy.write_bytes(b"policy fixture")
            dependencies = {"opencc-js": "1.4.1"}
            manifest = build_manifest(
                serial=7,
                node_major=18,
                runtime_lock=lock,
                runtime_policy=policy,
                dependencies=dependencies,
                core_versions={"stable": "1.20.0", "dev": "1.20.0"},
                core_commits={"stable": "a" * 40, "dev": "b" * 40},
                archive=archive,
                package_records=[
                    {
                        "name": "opencc-js",
                        "version": "1.4.1",
                        "integrity": "sha512-opencc",
                        "path": "node_modules/opencc-js",
                    }
                ],
                artifact_file_count=12,
                artifact_extracted_size=1024,
                excluded_packages=["drizzle-orm"],
            )
        archive_sha = hashlib.sha256(b"zip fixture").hexdigest()
        self.assertEqual(MANIFEST_SCHEMA, manifest["schema"])
        self.assertEqual(RUNTIME_PROTOCOL, manifest["runtimeProtocol"])
        self.assertEqual(7, manifest["serial"])
        self.assertEqual(
            {"stable": "1.20.0", "dev": "1.20.0"},
            manifest["coreVersions"],
        )
        self.assertEqual(
            {"stable": "a" * 40, "dev": "b" * 40},
            manifest["coreCommits"],
        )
        self.assertEqual(hashlib.sha256(b"policy fixture").hexdigest(), manifest["runtimePolicySha256"])
        self.assertEqual(12, manifest["artifactFileCount"])
        self.assertEqual(1024, manifest["artifactExtractedSize"])
        self.assertEqual(["drizzle-orm"], manifest["excludedPackages"])
        self.assertNotIn("channel", manifest)
        self.assertNotIn("entries", manifest)
        self.assertNotIn("files", manifest)
        self.assertEqual(archive_sha, manifest["artifactSha256"])
        self.assertTrue(manifest["artifactUrl"].endswith("/node_modules.zip"))
        self.assertEqual(
            dependency_fingerprint(dependencies), manifest["dependencyFingerprint"]
        )
        self.assertEqual(
            manifest,
            json.loads(canonical_json_bytes(manifest).decode("utf-8")),
        )

    def test_reads_version_from_core_globals(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = Path(tmp)
            globals_js = core / "danmu_api" / "configs" / "globals.js"
            globals_js.parent.mkdir(parents=True)
            globals_js.write_text("export default { VERSION: '1.20.0' };\n", encoding="utf-8")
            (core / "package.json").write_text('{"version":"1.0.0"}', encoding="utf-8")

            self.assertEqual("1.20.0", read_core_version(core))


if __name__ == "__main__":
    unittest.main()
