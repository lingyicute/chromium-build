# Chromium Stable Build

**仅使用 GitHub 官方 Actions 的分段构建方案。** 目标为 GitHub.com 的标准 `ubuntu-24.04` hosted runner，构建原版 Chromium `chrome_public_apk`。此方案依赖 runner 实际提供独立且够大的 `/mnt` 文件系统；GitHub 不保证该额外空间，环境不满足时会提前失败。

## 目录

```text
.github/
  workflows/
    chromium-stable.yml       # 手动触发、准备段（stage 00）、最多 8 个续编段、最终检查
    chromium-stage.yml        # 准备段与续编段共用的单段 job，不是 composite Action
    tests.yml                 # 每次 push 运行 tests/ 下的离线测试，不编译 Chromium
  scripts/
    common.sh                # 固定绝对路径、工具缓存位置与网络重试
    runner.sh                # hosted VM、独立 /mnt、目录类型/内容预检
    host.sh                  # 临时 VM 清理、/ 上建 swap、资源检查，不安装系统包
    resolve.py               # 解析一次 Android Stable，验证 JSON
    fetch.sh                 # 源码与工具链锁定、同步、hooks、GN
    checkpoint.py            # 分块清单摘要、逐块先验证后流式恢复
    pack.sh                  # 破坏性导出检查点（仅用于临时 VM）
    restore.sh               # 恢复并检查仓库/运行/提交/前序阶段
    relay.sh                 # 导出与恢复共享的 tar 操作
    source_state.py          # 源码锁、Git 工作树、版本与续编历史校验
    build.py                 # 按 job 截止点分配编译预算，监督 Ninja 进程会话
    build.sh                 # 编译日志
    release.sh               # APK 验签与产物整理
    apk.py                   # 安全暂存并检查构建生成的 APK
    confirm-upload.sh        # 确认成功上传返回了可安全解析的 artifact ID
config/
  args.gn
  depot_tools_revision.txt
  debug_signer_sha256.txt
tests/
  test_workflow.py
  test_runner.py
  test_apk.py
  test_checkpoint_integration.py
README.md
```
> [!tip]
> 请不要在个人机器或已有数据的自托管 runner 上执行本项目的 `host.sh` / `pack.sh`。前者删除固定路径下的预装大型软件，并关闭、删除 `/mnt` 下已有的 swap 文件；后者会在打包成功读取各文件后删除整个本地构建树。

## 运行

打开 **Actions → Chromium Stable Android arm64 → Run workflow**。

无需添加任何 repository secret。不需要 PAT、Google API key 或 keystore。

## 执行结构

```text
prepare（stage 00，与续编段共用 chromium-stage.yml）
  ├─ 清理 / 上的预装软件、在 / 建立 16 GiB swap（/mnt 整盘留给源码树与检查点）、检查空闲空间
  ├─ ChromiumDash 查询 Android Stable
  ├─ 官方 Git tag 的提交必须与查询返回的 Chromium SHA 相符
  ├─ checkout 固定的 depot_tools；禁用自更新
  ├─ gclient sync --nohooks → 官方 hooks → gn gen
  ├─ 保存来源与配置记录
  ├─ 用本 job 剩余时间直接开始本地 Ninja 编译（省去一次导出、下载与恢复）
  └─ 未完成：完整构建树 → tar/zstd 分块 → 本次运行 artifact；完成：验签并上传 APK
       ↓
part-01 → part-02 → … → part-08
  ├─ 按前序 job 给出的精确 artifact ID 下载
  ├─ 用前序 job 给出的 SHA-256 认证分块清单，逐块验证后才解包
  ├─ 恢复固定路径与纳秒文件时间戳，核对仓库、版本和完整前序记录
  ├─ 不重新 sync / runhooks / gn gen
  ├─ 本地 Ninja 编译至本 job 开始后第 260 分钟（最终段 part-08 为第 325 分钟）
  ├─ 正常到点未完成：常规段正常中断并导出新检查点；最终段跳过无用检查点直接结束
  ├─ 真正编译失败/OOM/停机失败：直接失败
  ├─ 完成：验证 APK 签名并上传产物，跳过后续分段
  └─ 检查点下载/上传与 APK 上传失败时各自动重试一次
       ↓
result：只接受连续的成功检查点，以及已确认上传 APK 的成功段
```

每个 job 上限 350 分钟。在首个输入校验步骤中、checkout 前记录单调时钟（`CLOCK_BOOTTIME`），常规编译段（含 `prepare`，源码同步越慢其编译时间越少）受到“本 job 开始后第 260 分钟停止”的预算约束，为导出和上传留出约 90 分钟；最终段 `part-08` 无后续消费者，跳过检查点打包上传并将截止点放宽至第 325 分钟，仅保留时间用于 APK 验签与产物上传。每段编译时长即截止点减去同步/下载/恢复耗时，不另设固定时间片上限。

每个步骤都设有 `timeout-minutes`：`continue-on-error` 只容忍失败，挡不住挂起。任一步骤（包括只作记录的 `source-lock-` / `logs-` 上传）卡住时只会让该步超时，不会一直拖到 job 上限。截止点为 Ninja 停机、会话清理和产物处理留出时间；按配置的步骤时限计算，常规段的首次检查点上传与最终段的 APK 路径均能为 job 初始化、步骤切换和收尾额外留下至少 4 分钟。检查点上传重试仍是尽力而为，日志在该重试前保存。

> [!tip]
> 这只是时间预算，慢网络下可能无法在 GitHub 的时限内保存检查点。

每段的 job summary 会列出编译前准备（清理、同步或下载与恢复）与编译各自的耗时、编译期间写入 swap 的数据量与编译结束时构建盘的剩余空间，以及检查点打包、恢复的体积与吞吐；`metadata/runners.jsonl` 同样记录 `setup_seconds`、`swap_out_mib` 与 `disk_free_gib`。编译失败时，swap 写入量与剩余空间也会写进错误信息，便于区分内存压力（OOM）、磁盘写满与真正的编译错误。跑过一次真实构建后，可据此判断约 90 分钟的预留是否过宽、`-j` 上限是否合适，再调整 `build.py` 中的截止点、并行度与工作流里的步骤时限。

检查点含源码、`.git`、GN/Ninja 中间产物、depot_tools、vpython 缓存和隐藏文件（临时 `CIPD_CACHE_DIR` 下载缓存在 `fetch.sh` 结束时直接清理，不进入检查点；`.gclient` 使用 `checkout_configuration="small"` 并关闭 JS 覆盖率模块与测试用 `generate_location_tags` 钩子以缩减体积，`config/args.gn` 锁定 `concurrent_links = 1` 防止并发链接 OOM）。归档使用 POSIX tar 保留纳秒 mtime、符号链接与权限（打包前仅为受限目录补齐 `u+rwx` 并清理残留 Unix 套接字以便删除），经 `zstd` 以全部逻辑 CPU 压缩（`-T0` 只按物理核计数，在每核双线程的 hosted runner 上只会用到一半 vCPU）后，边按 1 GiB 分块写入边计算每块 SHA-256，并生成记录各块摘要与大小的 `tree.manifest`；传给下一段的是该清单的 SHA-256。导出时删除已归档的源文件；恢复时先认证清单，再逐块“验证 → 解包 → 删除”：下一块在后台线程验证；当前块从已打开的文件描述符重读（通常命中页缓存），每 8 MiB 复核已验证的前缀摘要，再交给 zstd/tar。即使同一文件在两次读取间被改写也不会输出改变的字节。

Android stable 的版本不是写死的；每次新 workflow run 从官方 API 解析一次，再锁定源码提交。运行期间版本不会漂移。官方 tag 与 API 暂时不一致会失败，不会猜测另一个版本。

## 安全性

### 官方 Action 锁定

| Action | 版本 | 固定 commit |
|---|---|---|
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `actions/upload-artifact` | v7.0.2 | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` |
| `actions/download-artifact` | v8.0.2 | `9000827ccba6bdab643e8b6fd33ac0654aef8333` |

本方案没有任何第三方 Action。

### depot_tools

`depot_tools` 固定为：

```text
26b75c9b6bd10093b52b3dab6e77c48a84471b7c
```

该 revision 来自 Chromium 官方 depot_tools 仓库。

> [!tip]
> 若需要更新 depot_tools，应通过审查后的仓库提交更新 `config/depot_tools_revision.txt`，而不是在运行时启用自更新。

## 产物

运行页面的 Artifacts：

| 名称前缀 | 内容 |
|---|---|
| `apk-` | `Chromium-Stable-arm64.apk`、`apk-signature.txt`、`SHA256SUMS`、`metadata/` |
| `unverified-apk-` | 仅在编译完成、原始 APK 已安全暂存但后续步骤失败时可能出现：未经验证的原始 APK，仅供排查，不要安装或分发；只保留 3 天 |
| `source-lock-` | 初始版本/源码/工具链锁定记录、DEPS、GN 参数、依赖列表 |
| `logs-` | 资源信息与编译日志 |
| `tree-` | 续编译检查点，不是安装包；只保留 3 天。每段都会上传完整树，一次跑满 8 段的 run 检查点总量可达 200 GB 以上 |

`source-lock-` 与 `logs-` 只用于记录和排查：它们上传失败只留下告警，不会让该段失败、中断后续分段。

`metadata/build-lock.json` 包含 Chromium 版本与提交、depot_tools 提交、workflow 源码提交与 run ID。`metadata/runners.jsonl` 记录各编译阶段的 runner 镜像版本、并行度、swap 写入量与剩余磁盘空间。

本构建保留非官方 Chromium 的 `android_channel="default"`；这里 stable 指**源码版本**，并非表示本仓库产物是 Chrome 官方稳定版。非官方/非 Chrome 品牌构建默认开启 `dcheck_always_on`、`enable_java_asserts`、`debuggable_apks` 与 `fieldtrial_testing_config`，`config/args.gn` 显式将其全部关闭（并设 `disable_fieldtrial_testing_config = true`），避免 APK 带着会直接崩溃的 C++/Java 断言、`android:debuggable="true"` 或未发布测试实验运行（`release.sh` 亦会拒绝 debuggable APK）。Android 最低版本由所选 stable 源码决定，不硬编码向下兼容承诺。

**APK 使用公开开发密钥签名，不适合生产分发。** `config/debug_signer_sha256.txt` 锁定上游 `build/android/chromium-debug.keystore` 的证书 SHA-256，若上游更换该证书，应审查后更新。验签与证书匹配不能证明发布者身份，因为开发私钥是公开的。

## 故障与重跑

- `/mnt` 不独立或空间不足：当前 VM 不满足布局要求；不要通过关闭检查把整个树塞进系统盘。
- tag/SHA 不匹配：可能是上游发布记录/仓库更新的时间差，或元数据异常。检查官方记录，稍后启动新 run。
- `gclient` / hooks 失败：源码 fetch、`gclient sync` 与 hooks 会针对同一官方来源和版本各自最多尝试 3 次（不切换镜像或版本）；仍失败时查看 prepare 日志，网络问题可以重跑。
- `GN --fail-on-unused-args` 失败：新版上游可能调整参数，需要人工审查 `config/args.gn`。不静默忽略未知参数。
- artifact 摘要不符：停止；不要关闭验证。重新获取或重新构建，并核查生产者。
- artifact 传输失败：检查点下载、检查点上传与 APK 上传各自动重试一次；重新上传使用带 `-retry` 后缀的新 artifact 名（后续段按 artifact ID 取用，不受影响）。检查点上传的重试受 350 分钟 job 上限约束，属尽力而为。
- 编译完成但 `release.sh` 检查失败：job 仍判失败，如果原始 APK 是构建树内可安全暂存的普通文件，尝试另存为 `unverified-apk-` artifact，避免整次构建的结果只因一项检查而丢失。
- 编译 job 失败：在检查点仍存在的前提下，可尝试 GitHub 的 **Re-run failed jobs**。它仍属于原 run，artifact ID/输出必须可用；新 attempt 使用独立命名。若 GitHub 未保留所需 job 输出或 artifact 已过期，启动新 run。`prepare` 没有前序检查点，它在编译中失败时重跑会从源码同步重新开始。

## 参考

- [Chromium Android 构建说明](https://chromium.googlesource.com/chromium/src/+/main/docs/android_build_instructions.md)
- [Android Stable 官方发布查询](https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Android&num=1)
- [Chromium depot_tools](https://chromium.googlesource.com/chromium/tools/depot_tools/)
- [GitHub Ubuntu 24.04 runner 镜像清单](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
- [GitHub checkout 固定版本](https://github.com/actions/checkout/tree/3d3c42e5aac5ba805825da76410c181273ba90b1)
- [GitHub upload-artifact 固定版本](https://github.com/actions/upload-artifact/tree/cf430e030ddbb5b0abf93d22962f4752f3646cd9)
- [GitHub download-artifact 固定版本](https://github.com/actions/download-artifact/tree/9000827ccba6bdab643e8b6fd33ac0654aef8333)
