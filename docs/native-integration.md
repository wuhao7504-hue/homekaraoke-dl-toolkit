# 原生下载功能（HomeKaraoke DL 0.9.1）

在 `zalun/karaoke-app` 0.9.1 源码上真正实现了「搜索行里点一下就下载 / 离线曲库」，
编译出可用的 app：`build/HomeKaraoke DL.app`（ad-hoc 签名，26 MB）。

## 改了什么（18 个文件，+1272 行）

| 文件 | 内容 |
|---|---|
| `src-tauri/src/services/downloader.rs`（新，541 行） | 起 yt-dlp、`--newline` 逐行解析 `[download] 42.3% ...`、`emit("download:progress")`、进程登记表支持取消、`find_output` 按文件名核对产出（含「已存在被跳过」的情况）、`output_stem` 生成 `Artist - Title` 命名 |
| `src-tauri/src/commands/downloads.rs`（新） | `youtube_download`（立刻返回 download_id，后台任务跑）、`download_cancel`、`download_list`、`download_info`；解析下载目录（`download_dir` 设置 → 否则 `$AUDIO/HomeKaraoke`）、登记 `library_folders`、写 `videos` + `download_queue`、下载完自动跑一次 `library_scanner` 生成 `.hkmeta.json` 和缩略图 |
| `src-tauri/src/db/schema.rs` | **迁移 14**：`download_queue` 加 `file_path / quality / file_size_bytes / download_id` + status 索引（只加可空列，兼容已有库） |
| `src-tauri/src/lib.rs` | 注册 3 个新命令 |
| `src-tauri/src/services/keep_awake.rs`（新） | 用 `/usr/bin/caffeinate` 替换 `keepawake` crate —— 见下方「构建踩坑」 |
| `src/services/downloads.ts` / `src/stores/downloadStore.ts`（新） | invoke 封装 + `listen("download:progress")` + zustand 队列状态 |
| `src/components/download/DownloadPanel.tsx`（新） | 右下角浮动进度面板：进度条、速度/剩余、取消、清除完成、完成提示「重启后生效」 |
| `src/components/search/SearchResults.tsx` | 每行操作区加 `⤓` 下载按钮（不受「禁止嵌入」影响，照样能下） |
| `src/App.tsx` | 挂载 `DownloadPanel` |

补丁文件：`native-download.patch`（排除 pnpm-lock，可直接 `git apply` 到干净 clone）。

## 怎么用

1. 双击 `build/HomeKaraoke DL.app`（建议拖到 Dock）。
2. 搜索歌名 → 结果行右侧 `⤓` → 右下角出现进度面板。
3. 完成后写进 `videos`（`source_type='local'`）+ `download_queue`，并自动扫描生成封面/歌词。
4. 到 **Local** 标签即可离线播放。

### 下载目录解析顺序（`resolve_download_dir`）

1. `settings.download_dir`（显式设置，优先级最高）
2. **`library_folders` 里的第一个曲库目录** ← 默认走这里，下载即入库
3. 兜底 `<audio>/HomeKaraoke`

也就是默认和「下载器（外挂版）」写的是同一个位置：`~/Music/HomeKaraoke`。
启动/打开下载面板时会在 App 日志里写一行，例如：

```
[homekaraoke_lib::commands::downloads][INFO] download dir: ~/Music/HomeKaraoke (from library folder)
```

### 封面（缩略图）

- 扫描时由 ffmpeg 抽帧写到 `<曲库>/.homekaraoke/<文件名>.thumb.jpg`（宽 320），
  曲库浏览读 `thumbnail_path`（仅当文件存在）
- **修复的 bug**：下载完成后的自动扫描原先在 async 任务里调用
  `LibraryScanner::scan_folder`，而它内部会 `tokio::runtime::Runtime::new()` ——
  在已有 runtime 的线程上会 panic，所以日志里只有 `Found N video files` 就没了下文，
  缩略图和 `.hkmeta.json` 从来没生成过。现在改成 `spawn_blocking` 跑。
- 前端兜底：`thumbnail_path` 为空但有 `youtube_id` 时，直接用
  `https://i.ytimg.com/vi/<id>/mqdefault.jpg` 显示封面（CSP `img-src https:` 允许）
- 已为现有 27 首批量补生成 `.thumb.jpg`

### 播放时预缓存下一首

播放任意一首歌时，前端会把**队列里接下来的 3 首**（`PREFETCH_AHEAD = 3`）悄悄下载到曲库（720p，静默，不弹通知）：

- 预取走 `Priority::Background`：只在没有用户下载排队时占用下载槽位，你手动点的 ⤓ 永远排在它前面；最多让位等待 10 分钟后按普通优先级排队

- 触发点：`App.tsx` 里监听 `currentVideo` 变化 → 取 `queue[0]` → `usePrefetchStore.prefetchNext`
- 已经存在于磁盘的（`download_cached_path` 查 `videos.file_path`）直接跳过
- 轮到下一首时优先走本地文件：`handlePlayFromQueue`、`handlePlay`、`VideoPlayer.handleEnded`
  三处都会先查缓存命中，命中就以 `source: 'local'` 起播（不重新走 YouTube 流，无缓冲、可离线）
- 开关：下载面板里的「缓存下一首」勾选框（存在 localStorage `karaoke:prefetch-next-song`）

日志里能看到 `[Prefetch] prefetching next: …` 和 `Playing prefetched file from queue: …`。

### 稳健模式 + 断路器 + POT 探测

下载面板里有「稳健」勾选框（默认开，`settings.download_robust`），开启后：

| 参数 | 普通模式 | 稳健模式 |
|---|---|---|
| `--concurrent-fragments` | 4 | **1** |
| `--http-chunk-size` | — | **10M**（强制 Range 分段） |
| `--sleep-interval` / `--sleep-requests` | — | **2 / 1 秒** |
| 403 重试 | 冷却 8s 重试 1 次 | **指数退避 8s → 30s → 120s**，每次重跑 yt-dlp（即重新解析拿新签名直链） |

**断路器**：5 分钟内累计 3 次 403 → 全局冷却 15 分钟（`cooldown_remaining()`），期间用户点下载会直接被拒并提示「403 次数过多，下载已冷却，还需等 N 秒」，面板显示倒计时；预取任务在冷却期静默跳过。

**POT 探测**：启动下载前探测 `127.0.0.1:4416`（bgutil PO token provider），在跑就自动加
`--extractor-args youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416`，面板显示 `POT 开/无`。

相关日志：`download dir … (from library folder)` / `download cookies: chrome` / `403，30s 后重试 2/3`。

### Cookie（YouTube 机器人验证）

YouTube 会对「同 IP 短时间内多次抓取」触发 `Sign in to confirm you're not a bot`，
yt-dlp 拿不到任何格式。必须带上已登录 YouTube 的浏览器 Cookie：

- 设置项 `download_cookies`：`none` | `chrome` | `safari` | `edge` | `firefox`（默认 `chrome`）
  → 传 `--cookies-from-browser <浏览器>`
- 设置项 `download_cookies_file`：指向 Netscape 格式 `cookies.txt` → 传 `--cookies <路径>`
- 下载面板里有下拉框，失败时提示会直接告诉你改这里

macOS 细节：Chrome 的 Cookie 用「Chrome Safe Storage」钥匙串条目加密，App 首次读取时会弹一次授权
（选「始终允许」）；Safari 的 Cookie 库属于受保护目录，需要给 App「完全磁盘访问权限」。

### 质量

`settings.download_quality`：`1080p`（默认）| `720p` | `audio`。

下载面板顶部会显示当前目标目录，鼠标悬停还能看到质量。

## 下载/缓存健壮性修复（第二轮）

| 问题 | 现象 | 修法 |
|---|---|---|
| 输出文件按**前缀**匹配 | `稻香.mp4` 会把 `稻香 (live).mp4` 当成自己的产出，或误报成功 | `find_output` 改为精确匹配 `<stem>.<ext>`，并加单测 |
| 同一首重复起进程 | 连点同一个 ⤓ 或预取撞上手动下载 → 两个 yt-dlp 抢同一个输出 | 后端 `inflight` 表按 `video_id` 去重，第二次直接返回已有 `download_id` |
| 下载后扫描在 async 里建 runtime | 扫描在 `Found N video files` 后静默中断，缩略图/hkmeta 从不生成 | 改用 `spawn_blocking` |
| 每首下载都全库扫 | 批量下载时扫描堆积、拖慢 | 45 秒内只扫一次（throttled） |
| `download_id` 列被塞了标记值 | `download_queue.download_id` 里写着 `restart-needed` 这种脏数据 | 改为写入真实 download id |
| 失败没有记录 | 面板只能看到通知，历史查不到 | 失败也写 `download_queue`（status=failed + error_message） |
| 预取在冷却期继续撞 | 冷却中仍发起 yt-dlp | `Priority::Background` 在冷却期直接跳过 |
| 缓存映射不更新 | 下载完成后仍要重新查库；文件删了仍命中旧路径 | 完成事件 `prime(videoId, path)`，失败 `release`，删除用 `forget` |

## 验证记录

| 检查 | 结果 |
|---|---|
| `cargo check` / `cargo test --lib downloader::` | 5 个单测通过（进度解析、命名、argv 构造） |
| 真实下载 e2e（`--ignored`，mock AppHandle） | `周杰倫 - 稻香.m4a` 3,634,427 bytes，命名与落盘正确 |
| Cookie 通路 | `--cookies-from-browser chrome` → `Extracted 2642 cookies`，之前报 bot 校验的视频可正常取流 |
| `tsc -b` / `eslint` | 通过 |
| 构建 | `HomeKaraoke DL.app` 25.82 MiB，ad-hoc 签名 |
| 启动 | 日志无 panic；`asset protocol scope` 已加 `~/Music/HomeKaraoke` |
| 迁移 | `schema_version = 14`，`download_queue` 新增 4 列 |
| 库内数据 | 3 首 local（此前下载器写进去的，被原生版直接识别） |

## 登录（Supabase）配置

`.env` 被仓库 `.gitignore` 忽略，所以**从源码构建时默认没有 Supabase 配置**，表现为：
OAuth 流程能走完、token 也写进钥匙串，但界面一直显示未登录 —— 日志里是

```
[AuthStore] Supabase not configured, skipping profile fetch
```

修法：写入 `upstream/.env`（客户端公开配置，与官方包一致）：

```
VITE_SUPABASE_URL=https://<project-ref>.supabase.co
VITE_SUPABASE_ANON_KEY=<publishable-key-from-released-app>
```

这两个值是从官方 0.9.0 的内嵌前端资源里读出来的（`http://localhost:14200/assets/index-*.js`），
属于 publishable key，本身就是给客户端用的公开值。

另外：两个 App 共用 `homekaraoke://` scheme 和同一个 bundle id，OAuth 回调可能被投递给
官方版；只保留一个在跑（或把 `/Applications/HomeKaraoke.app` 挪走）最稳。

## 构建踩坑（复现时看这里）

1. **`apple-sys` 编译失败**：`keepawake` → `apple-sys` 在编译期用本机 SDK 生成 IOKit/CoreFoundation 绑定，
   新版 Xcode（clang 21）产出尾部悬空属性 → `error: expected item after attributes`。
   处理：去掉 `keepawake`，用 `caffeinate` 子进程实现保活（行为一致，还少一层依赖）。
2. **`pnpm tauri` 报 `unrecognized subcommand '/Applications/DSH Desktop.app/...'`**：
   `@tauri-apps/cli` 的 JS 包装器用 `process.argv[0]` 的 basename 猜包管理器，这个环境的 node 是
   Electron Helper 的 shim，猜错后把二进制路径当成子命令塞进参数。
   处理：绕开包装器，直接 `node -e 'require("@tauri-apps/cli/main").run([...])'` 调 CLI。
3. **签名**：`tauri.conf.json` 里原本是作者的 Developer ID，本地改成 `signingIdentity: "-"`（ad-hoc），
   首次打开需在「系统设置 → 隐私与安全性」点「仍要打开」。
4. **产物名**：`productName` 改成 `HomeKaraoke DL`，避免覆盖官方 0.9.0；`identifier` 保持
   `app.homekaraoke` 不变，因此**共用同一个 `karaoke.db` 和曲库**（这是有意的，升级即继承数据）。

## 已知边界

- 不做断点续传 UI（yt-dlp 自身支持 `--continue`，重下即续）；并发为「一命令一进程」，没有全局队列限流。
- 下载目录若改成 asset scope（`$AUDIO/**`、`$VIDEO/**`）之外，需要改 `tauri.conf.json` 重新构建。
- 与官方版本共用 bundle id，`open -a HomeKaraoke` 可能打开任意一个；用 Dock/路径启动更稳。
- 上游若继续演进（0.9.2+），`native-download.patch` 需要 rebase。
