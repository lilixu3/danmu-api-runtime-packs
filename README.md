# danmu-api Android Runtime Packs

This repository publishes **derived Android pure-JavaScript runtime dependency
packs** for the Danmu App. It does not mirror or modify either core repository.

## Trusted channels

Only these exact sources are accepted:

| Channel | Repository | Branch | Signed index |
|---|---|---|---|
| `stable` | `huangxd-/danmu_api` | `main` | `stable/index.json` + `stable/index.sig` |
| `dev` | `lilixu3/danmu_api` | `main` | `dev/index.json` + `dev/index.sig` |

The builder accepts only `--channel stable` or `--channel dev`; repository and
branch values come from its hard-coded allowlist. Arbitrary custom repositories
are never cloned or packaged by the signing pipeline.

The root `index.json` and `index.sig` remain a schema-1 **stable compatibility
index** for older App versions. Stable schema-2 output is converted into a distinct
schema-1 manifest/ZIP and all archive and manifest hashes are recomputed before that
legacy index is signed. Development packs never enter the legacy index.

## Channel isolation

Stable and development packs are intentionally separate even when both core
commits currently have the same dependency fingerprint. Each signed index,
entry, manifest, Release tag, and asset carries the channel and exact source:

```text
stable-core-<sha12>/runtime-pack-stable-<sha12>.zip
dev-core-<sha12>/runtime-pack-dev-<sha12>.zip
```

The new App reads only the index matching its selected built-in core variant.
There is no stable-to-dev, dev-to-stable, or custom-core fallback. If a matching
signed pack is unavailable, the App aborts before live-core replacement and
keeps the old core.

## Security boundary

Resolution and `worker.js` smoke run under Node.js 18.20.4 in a read-only build
job without the signing private key or repository write credentials. A separate
publish job receives only the verified output, publishes the immutable Release
asset, updates the selected channel index, and signs it. If a same-SHA Release
already exists, the job downloads it and requires byte-for-byte equality; a
mismatch fails before the index is changed, and `--clobber` is forbidden.

The builder rejects:

- `preinstall`, `install`, or `postinstall` lifecycle scripts;
- `.node`, `.so`, `.dll`, `.dylib`, `binding.gyp`, and prebuilt binaries;
- `os`, `cpu`, or `libc` constrained packages;
- package-internal symbolic links;
- Git/file/http/workspace/link/npm-alias or shorthand Git dependency specs.

The App verifies the exact signed index bytes, channel, source repository and
branch, archive URL/size/SHA-256, manifest, Node major, dependency fingerprint,
runtime lock, package list, and every extracted file hash.

## Policies

Channel policy files live under:

```text
policies/stable.json
policies/dev.json
```

Both currently exclude Android-non-runtime direct dependencies `chokidar`,
`dotenv`, `esbuild`, and optional `redis`. Unsafe package signals are enforced in
code and cannot be disabled by a policy file.

## Local build

Stable:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_runtime_pack.py \
  --core-dir /path/to/huangxd-danmu_api \
  --channel stable \
  --core-sha <full-stable-sha> \
  --output-dir dist \
  --policy policies/stable.json \
  --node-major 18
```

Development:

```bash
python3 scripts/build_runtime_pack.py \
  --core-dir /path/to/lilixu3-danmu_api \
  --channel dev \
  --core-sha <full-dev-sha> \
  --output-dir dist \
  --policy policies/dev.json \
  --node-major 18
```

## Scheduling

- Stable checks `huangxd-/danmu_api@main` at minute 17 every two hours.
- Development checks `lilixu3/danmu_api@main` at minute 47 every two hours.
- Manual `force` dispatch remains available per channel for diagnostics or recovery
  when that SHA's Release does not yet exist; it never overwrites an existing asset.

Historical immutable packs from these two sources are retained for rollback;
no third repository is admitted.
