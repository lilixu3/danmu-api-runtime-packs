# DanmuApiApp Android Runtime Dependencies

This repository publishes one shared, pure-JavaScript `node_modules.zip` for
the stable and development cores used by DanmuApiApp.

## Published files

The current signed metadata is kept at the repository root:

```text
manifest.json
manifest.sig
runtime-pack-public-key.pem
```

`manifest.json` points to one immutable GitHub Release asset named
`node_modules.zip`. The ZIP contains exactly one top-level `node_modules/`
directory. The App verifies the manifest signature, protocol and Node version,
then verifies the archive size and SHA-256 before extraction.

## Dependency source

`runtime/package.json` is the canonical Android runtime allowlist and uses exact
versions. `runtime/package-lock.json` pins the complete transitive closure. The
current direct dependencies are:

- `@dan-uni/dan-any`
- `brotli`
- `https-proxy-agent`
- `node-fetch`
- `opencc-js`
- `pako`

The publish workflow checks both `huangxd-/danmu_api@main` and
`lilixu3/danmu_api@main`. It fails if either core adds a required dependency not
covered by the common runtime. Android-non-runtime dependencies `chokidar`,
`dotenv`, `esbuild`, and optional `redis` are intentionally excluded.

## Security checks

The builder rejects install lifecycle scripts, native binaries, platform-bound
packages, prebuild directories, symbolic links, and non-registry dependency
specifications. Both core `worker.js` entry points are smoke-tested with the
locked dependency closure before publishing.

The private signing key is available only to the publish job. The App embeds
`runtime-pack-public-key.pem` and accepts only the exact signed manifest bytes.
The manifest carries a monotonically increasing serial to reject rollback.

## Local verification

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_runtime_pack.py \
  --runtime-dir runtime \
  --stable-core-dir /path/to/huangxd-danmu_api \
  --dev-core-dir /path/to/lilixu3-danmu_api \
  --output-dir dist \
  --serial 1 \
  --node-major 18
```

The generated `dist/node_modules.zip` is deterministic for a fixed lockfile.
