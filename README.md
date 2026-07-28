# DanmuApiApp Android 运行时依赖

本仓库为 DanmuApiApp 发布一个稳定版和开发版共用的 Android 精简
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
`runtime/package-lock.json` 固定完整传递依赖闭包；
`runtime/android-runtime-policy.json` 则固定经过人工确认的 Android 包集合、
版本绑定裁剪规则、关键文件和体积预算。当前直接依赖如下：

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

## Android 精简策略

构建器先按锁文件安装完整生产依赖，再复制 Android 候选目录并执行以下裁剪：

- `dan-any` 的 PGlite/Drizzle 数据库实现不属于核心使用的 `core/main/pure`
  路径，因此排除 `@electric-sql/pglite`、`@electric-sql/pglite-tools` 和
  `drizzle-orm`；
- 在打包副本中把上述依赖移到 `dan-any` 的 `optionalDependencies`，使现有 App
  的本地 ZIP 闭包校验仍可识别该精简包；
- `opencc-js` 只保留核心实际导入的简繁转换模块、字典和许可证；
- 删除 source map、类型声明、说明文档和 Pako 重复发行文件。

排除项、文件白名单和被裁剪包的核心导入入口都经过审核。包新增、删除、升级，核心改用
未经确认的 Dan-any/OpenCC 入口，或者产物超出包数量、文件数量、解压体积、ZIP 体积
预算时，构建会以差异信息失败，必须人工更新策略后才能发布。
生成的 `build-report.json` 会列出完整依赖与 Android 精简结果的包数、文件数和体积。

## 安全校验

构建器会拒绝以下内容：

- `preinstall`、`install`、`postinstall` 安装脚本；
- `.node`、`.so`、`.dll`、`.dylib` 等原生文件；
- 带操作系统、CPU 或 libc 限制的包；
- `prebuilds`、符号链接和非 npm registry 依赖。

裁剪完成后，构建器会先执行 Dan-any pure/adapters、OpenCC、Brotli、Pako、
node-fetch 和代理模块的功能 smoke，再分别启动稳定版与开发版真实核心的
`worker.js`。私钥只提供给独立的签名发布任务，构建任务没有仓库写权限和签名私钥。

工作流每两小时读取两个核心的最新提交 SHA。任一 SHA、锁文件、精简策略或构建器
发生变化都会重新生成精简候选并执行全部测试；只有 ZIP 内容哈希变化时才会创建新的
依赖 Release，内容未变化时只更新签名兼容性清单。新包和清单提交成功后，会删除标题
相同的旧哈希 Release，因此每组核心版本在仓库中只保留当前有效的一份依赖包。

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

锁文件、策略和构建器不变时，生成的 `dist/node_modules.zip` 字节内容和 SHA-256
保持一致。`dist/build-report.json` 可用于审核本次精简前后的差异。
