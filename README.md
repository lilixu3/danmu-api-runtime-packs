# DanmuApiApp Android 运行时依赖

本仓库为 DanmuApiApp 发布一个稳定版和开发版共用的纯 JavaScript
`node_modules.zip`。仓库不保存核心源码，也不执行核心中的安装脚本。

## 发布内容

仓库根目录只保留当前版本的签名元数据：

```text
manifest.json
manifest.sig
runtime-pack-public-key.pem
```

`manifest.json` 指向 GitHub Release 中不可变的 `node_modules.zip`。压缩包只有
一个顶层 `node_modules/` 目录。Release 标题直接使用对应核心版本号；稳定版和
开发版版本不同时，会同时显示两个版本。

App 会依次校验清单签名、协议版本、Node.js 主版本、压缩包大小和 SHA-256，
全部通过后才会解压并安装。清单序号只增不减，用于拒绝旧清单回放。

## 依赖来源

`runtime/package.json` 是 Android 运行时直接依赖清单，全部使用精确版本；
`runtime/package-lock.json` 固定完整传递依赖闭包。当前直接依赖如下：

- `@dan-uni/dan-any`
- `brotli`
- `https-proxy-agent`
- `node-fetch`
- `opencc-js`
- `pako`

发布工作流会同时检查以下两个核心的 `main` 分支：

- 稳定版：`huangxd-/danmu_api`
- 开发版：`lilixu3/danmu_api`

任一核心新增未覆盖的运行时依赖都会使发布失败。仅用于服务端监听、构建或可选
缓存的 `chokidar`、`dotenv`、`esbuild` 和 `redis` 不进入 Android 依赖包。

## 安全校验

构建器会拒绝以下内容：

- `preinstall`、`install`、`postinstall` 安装脚本；
- `.node`、`.so`、`.dll`、`.dylib` 等原生文件；
- 带操作系统、CPU 或 libc 限制的包；
- `prebuilds`、符号链接和非 npm registry 依赖。

发布前会使用锁定依赖分别启动测试两个核心的 `worker.js`。私钥只提供给独立的
签名发布任务，构建任务没有仓库写权限和签名私钥。

## 本地校验

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

锁文件不变时，生成的 `dist/node_modules.zip` 字节内容和 SHA-256 保持一致。
