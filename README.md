# danmu-api Android Runtime Packs

This repository publishes **derived Android runtime dependency packs** for the
Danmu App. It does not mirror or replace the core source repository.

## Source of truth

The only supported upstream core source is:

```text
https://github.com/huangxd-/danmu_api.git
```

The `coreRepo` value in every generated entry must remain exactly:

```text
huangxd-/danmu_api
```

The App must never use this repository to treat `lilixu3/danmu_api` as the
stable upstream core.

## Pack contract

Each pack is keyed by the full upstream commit SHA and contains only the
Android pure-JavaScript dependency tree. The App verifies the signed index,
archive SHA-256, embedded manifest, package list, and file hashes before
activating a core update.

The current policy intentionally excludes core dependencies that are server-
only, build-only, or optional for Android (`chokidar`, `dotenv`, `esbuild`, and
`redis`). A package with lifecycle install scripts, native artifacts, platform
constraints, or prebuilt binaries is rejected rather than silently installed.

## Local build

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_runtime_pack.py \
  --core-dir /path/to/danmu_api \
  --core-repo huangxd-/danmu_api \
  --core-sha <full-upstream-sha> \
  --output-dir dist \
  --policy policy.json
```

The scheduled workflow checks the official upstream `main` branch, builds a
pack for a new commit, publishes an immutable GitHub Release asset, updates
`signed index.json`, and signs the index with the repository secret.
