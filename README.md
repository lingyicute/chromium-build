# Chromium Stable Build

**仅使用 GitHub 官方 Actions 的分段构建方案。** 适用于 GitHub.com 的标准 `ubuntu-24.04` hosted runner；构建原版 Chromium `chrome_public_apk`。

## 目录

```text
.github/
  workflows/
    chromium-stable.yml       # 手动触发、准备、最多 8 段、最终检查
    chromium-stage.yml        # 本仓库内复用的单段 job，不是 composite Action
  scripts/
    common.sh                # 固定绝对路径与工具缓存位置
    host.sh                  # 临时 VM 清理和资源检查，不安装系统包
    resolve.py               # 解析一次 Android Stable，验证 JSON
    fetch.sh                 # 源码与工具链锁定、同步、hooks、GN
    checkpoint.py            # 严格摘要检查与分块流恢复
    pack.sh                  # 破坏性导出检查点（仅用于临时 VM）
    restore.sh               # 恢复并检查仓库/运行/提交身份
    build.py                 # 时间片与进程组监督
    build.sh                 # 编译日志
    release.sh               # APK 验签与产物整理
config/
  args.gn
  depot_tools_revision.txt
tests/
  test_workflow.py
README.md
```
> [!tip]
> 请不要在个人机器或已有数据的自托管 runner 上执行本项目的 `host.sh` / `pack.sh`。前者删除固定路径下的预装大型软件；后者会在打包成功读取各文件后删除整个本地构建树。

## 运行

打开 **Actions → Chromium Stable Android arm64 → Run workflow**，

无需添加任何 repository secret。不需要 PAT、Google API key 或 keystore。

## 执行结构

```text
prepare
  ├─ 清理临时 runner、建立 swap、检查空闲空间
  ├─ ChromiumDash 查询 Android Stable
  ├─ 官方 Git tag 的提交必须与查询返回的 Chromium SHA 相符
  ├─ checkout 固定的 depot_tools；禁用自更新
  ├─ gclient sync --nohooks → 官方 hooks → gn gen
  ├─ 保存来源与配置记录
  └─ 完整构建树 → tar/zstd 分块 → 本次运行 artifact
       ↓
part-01 → part-02 → … → part-08
  ├─ 按前序 job 给出的精确 artifact ID 下载
  ├─ 验证整个压缩流 SHA-256 后再解包
  ├─ 恢复固定路径与纳秒文件时间戳
  ├─ 不重新 sync / runhooks / gn gen
  ├─ 本地 Ninja 时间片：最多 210 分钟
  ├─ 正常到点未完成：正常中断、导出新检查点
  ├─ 真正编译失败/OOM/停机失败：直接失败
  └─ 完成：验证 APK 签名并上传产物，跳过后续分段
       ↓
result：只有确实完成且必要 job 未失败才算成功
```

每个 job 上限 350 分钟。编译还受到“本 job 开始后第 270 分钟停止”的预算约束，为导出和上传留出时间。

> [!tip]
> 这只是时间预算，慢网络下可能无法在 GitHub 的时限内保存检查点。

检查点含源码、`.git`、GN/Ninja 中间产物、depot_tools、CIPD/vpython 缓存和隐藏文件。归档使用 POSIX tar 保留纳秒 mtime、符号链接与权限，防止恢复后把所有输入误判为更新。使用 1 GiB 分块；导出时删除已归档的源文件，恢复时在摘要全部验证后逐块消耗/删除本地压缩分块，降低磁盘峰值。

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
| `apk-` | `Chromium-Stable-arm64.apk`、`apk-signature.txt`、`SHA256SUMS`、`metadata/`、`BUILD-NOTICE.md` |
| `source-lock-` | 初始版本/源码/工具链锁定记录、DEPS、GN 参数、依赖列表 |
| `logs-` | 资源信息与编译日志 |
| `tree-` | 续编译检查点，不是安装包；只保留 3 天 |

`metadata/build-lock.json` 包含 Chromium 版本与提交、depot_tools 提交、workflow 源码提交与 run ID。`metadata/runners.jsonl` 记录各编译阶段的 runner 镜像版本和并行度。

本构建保留非官方 Chromium 的 `android_channel="default"`；这里 stable 指**源码版本**，并非表示本仓库产物是 Chrome 官方稳定版。Android 最低版本由所选 stable 源码决定，不硬编码向下兼容承诺。

**默认 APK 使用公开开发密钥签名。** 

## 故障与重跑

- tag/SHA 不匹配：可能是上游发布记录/仓库更新的时间差，或元数据异常。检查官方记录，稍后启动新 run。
- `gclient` / hooks 失败：查看 prepare 日志；网络失败可以重跑。
- `GN --fail-on-unused-args` 失败：新版上游可能调整参数，需要人工审查 `config/args.gn`。不静默忽略未知参数。
- artifact 摘要不符：停止；不要关闭验证。重新获取或重新构建，并核查生产者。
- 编译 job 失败：在检查点仍存在的前提下，可尝试 GitHub 的 **Re-run failed jobs**。它仍属于原 run，artifact ID/输出必须可用；新 attempt 使用独立命名。若 GitHub 未保留所需 job 输出或 artifact 已过期，启动新 run。

## 参考

- [Chromium Android 构建说明](https://chromium.googlesource.com/chromium/src/+/main/docs/android_build_instructions.md)
- [Android Stable 官方发布查询](https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Android&num=1)
- [Chromium depot_tools](https://chromium.googlesource.com/chromium/tools/depot_tools/)
- [GitHub Ubuntu 24.04 runner 镜像清单](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
- [GitHub checkout 固定版本](https://github.com/actions/checkout/tree/3d3c42e5aac5ba805825da76410c181273ba90b1)
- [GitHub upload-artifact 固定版本](https://github.com/actions/upload-artifact/tree/cf430e030ddbb5b0abf93d22962f4752f3646cd9)
- [GitHub download-artifact 固定版本](https://github.com/actions/download-artifact/tree/9000827ccba6bdab643e8b6fd33ac0654aef8333)
