# zalun/karaoke-app（HomeKaraoke）源码分析

分析对象：`https://github.com/zalun/karaoke-app`，本地副本 `upstream/`（`git clone --depth 1`）
HEAD：`af04ff26` · 2026-10-20 · `fix: offer yt-dlp alongside the API key when search is not configured (#255)`
仓库版本号：**0.9.1**（你机器上装的是 0.9.0 DMG）

---

## 一、项目画像

| 维度 | 事实 |
|---|---|
| 许可证 | MIT |
| 技术栈 | Tauri 2（Rust）+ React 18 + TypeScript + Zustand + Tailwind + Vite |
| 代码量 | Rust 12,507 行（`src-tauri/src`，29 个文件）；TS/TSX 35,726 行（`src`，约 120 个文件） |
| 命令面 | 92 个 `#[tauri::command]`，全部在 `src-tauri/src/lib.rs:267` 起的 `generate_handler!` 里注册 |
| 数据库 | SQLite（rusqlite），`db/schema.rs` 里 **13 段编号迁移** + `schema_version` 表 |
| 测试 | 31 个 vitest 文件（stores / components），Rust 侧以模块内 `#[test]` 为主 |
| 构建 | `justfile`：`pnpm tauri build`；签名身份写死在 `tauri.conf.json`（作者 Developer ID） |

## 二、分层结构

```
React UI (src/components, src/stores)
   │  invoke("cmd", {...})   ← @tauri-apps/api
   ▼
Rust commands (src-tauri/src/commands/*.rs)   92 个，薄封装 + 日志 + CommandError
   │
   ▼
services/  ← 真正的业务逻辑
   ├── ytdlp.rs (550 行)            yt-dlp 发现 / 搜索 / 直链 / 视频信息
   ├── youtube_api.rs               Data API v3 搜索
   ├── library_scanner.rs (940+ 行) 本地曲库扫描、命名解析、.hkmeta、缩略图
   ├── ffmpeg.rs                    ffprobe 时长/年份、抽帧生成缩略图
   ├── metadata_fetcher.rs          MusicBrainz + Lrclib 歌词
   └── media_controls.rs / display_watcher.rs   macOS 媒体键、显示器热插拔
   │
   ▼
SQLite (db/mod.rs + db/schema.rs)
```

窗口模型：主窗口 + 可拆出的 display 窗口（`windowManager.ts`、`commands/display.rs`、`window_state` 表记录每个显示器上的位置），这是它作为「KTV 双屏」应用的核心设计。

## 三、与我们相关的表

| 表 | 作用 | 关键点 |
|---|---|---|
| `videos` | 曲目总表 | `source_type IN ('youtube','local','external')`，本地与流媒体共用一张表，靠 `file_path` + `youtube_id` 区分 |
| `library_folders` | 扫描根目录 | **启动时**把这些目录注册进 asset 协议 scope（`lib.rs` 的 “Setting up asset protocol scope”） |
| `download_queue` | 下载队列 | **表结构齐全但全仓只有 1 处引用——就是 `CREATE TABLE` 本身**：`youtube_id / status / progress_percent / error_message / completed_at`，没有任何 SQL 读写它 |
| `queue_items` | 排歌 + 历史 | 冗余存了 video 快照，`song_type` 用 `source` 区分 |
| `singers` / `singer_favorites` | 歌手与收藏 | 收藏也被冗余成视频快照，可反查 youtube_id |

## 四、YouTube 的三条链路

1. **iframe 播放（默认）**：`PLAYBACK_MODE=youtube`，直接嵌 YouTube 播放器。
   `DependencyCheck.tsx:44` 的逻辑是「iframe 模式 → 跳过 yt-dlp 检查」，这就是你库里
   `ytdlp_available=false` 却一切正常的原因。
2. **yt-dlp 直链播放**：`playerStore.playVideo` 判断
   `PLAYBACK_MODE === 'ytdlp' && ytDlpAvailable`，否则回退 iframe。
   走 `youtube_get_stream_url` → `ytdlp.rs:352 get_stream_url`：
   macOS 优先要 **HLS master manifest**（WebKit 原生可放），拿不到才退回 DASH 分离流
   （video URL + audio URL 两条，前端 `NativePlayer` + `useSyncedAudio` 同步播放）。
3. **搜索**：`youtube_get_search_method` 决定用 Data API v3（需 Key）还是 yt-dlp
   （`--dump-json --flat-playlist`）。最新的 #255 提交就是「没配 Key 时也允许用 yt-dlp 搜」。

## 五、下载功能的确切状态：**上游还没做**

- `plan/06-downloads.md`：`Status: Planned`，5 个任务全部未勾选；连命令签名都设计好了：
  `youtube_download(video_id)`（发进度事件）、`library_get_all`、`library_delete_video`；
  schema 草案就是我们看到的 `download_queue` + `videos`。
- `plan/archive/offline-mode.md`（287 行，已归档）：更完整的一版离线模式设计——`downloads` 表
  （含 `file_size_bytes`、`quality`、`thumbnail_path`）、带优先级的队列、磁盘配额管理、
  网络探测自动切离线、离线优先播放。**被移进 archive，说明做过一轮设计又收回**。
- 代码事实：全仓 `grep -i download` 只命中「应用更新下载按钮」；`ytdlp.rs` 只有
  `is_available / search / get_stream_url / get_video_info / install`，**没有下载**。

结论：不是作者不会做，是**服务层、表结构、设计文档都铺好了，最后一公里没写**。这也解释了
为什么我们只能做外挂：官方 DMG 里压根没有这条命令，前端也没有下载入口可点。

## 六、要原生实现，改哪几个文件

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src-tauri/src/services/downloader.rs`（新） | `tokio::process::Command` 起 yt-dlp：`-f bv*[ext=mp4]+ba[ext=m4a]/b --newline -o "<dir>/<Artist> - <Title>.%(ext)s"`；逐行读 `[download]  12.3%` 解析进度；`app.emit("download:progress", payload)`；保存 `Child` 句柄支持取消；`--continue` 断点续传 |
| 2 | `src-tauri/src/commands/downloads.rs`（新） | `youtube_download(video_id, title, artist)`、`download_cancel(id)`、`download_list()`、`download_retry(id)`；错误统一走 `CommandError` |
| 3 | `src-tauri/src/lib.rs` | `generate_handler!` 加 4 个命令；`app.manage(DownloadManager::default())` |
| 4 | `src-tauri/src/db/schema.rs` | 追加**迁移 14**：`ALTER TABLE download_queue ADD COLUMN file_path/file_size_bytes/quality`（表已存在，只加列即可，兼容你现有 0.9.0 库） |
| 5 | `tauri.conf.json` | 落盘目录必须在 asset scope 内：现在只允许 `$AUDIO/**`、`$VIDEO/**`。默认写 `$AUDIO/HomeKaraoke` 就免改配置；要支持自定义目录就得改 scope 并重新构建 |
| 6 | `library_scanner.rs` | 下载完调 `scan_folder`（`commands/library.rs:187` 已有 `library_scan_folder`），或直接写 `videos` 行。**命名必须是 `Artist - Title.mp4`**，因为 `parse_filename`（第 702 行）按第一个 `" - "` 切分 |
| 7 | `src/services/downloads.ts`（新） | `invoke` 封装 + `listen("download:progress")` |
| 8 | `src/stores/downloadStore.ts`（新） | 队列状态、进度、错误 |
| 9 | `src/components/search/SearchResults.tsx` | 每行按钮组（333–386 行）加「下载」；`Sidebar.tsx` 加「下载中」入口 |
| 10 | `capabilities/default.json` | 现在只开了 `core / dialog / fs:allow-read-file / http / log / opener`。落盘交给 Rust 侧做就不需要 `fs` 写权限；若要让前端写文件才需要加 `fs:allow-write-file` |

构建门槛：本机**没有 cargo/rustc**（`which cargo` 为空），需要 rustup（约 1.5 GB，首次编译
10–20 分钟）；Node 24 + pnpm 已在；Xcode 已在。签名身份是作者的 Developer ID，本地构建要改成
`codesign -s -`（ad-hoc）或删掉 `signingIdentity` 字段。

## 七、两条路线的取舍

| | 外挂（现在这套） | 原生补丁 |
|---|---|---|
| 上手成本 | 已完成，双击可用 | 装 rustup + 改 10 个文件 + 编译 |
| 界面一致性 | 独立窗口，风格刻意做过对齐 | 完全一致，按钮就在搜索行里 |
| 升级官方版本 | 不受影响 | 每次升级都要 rebase 补丁，或放弃官方更新 |
| 依赖 | yt-dlp/ffmpeg 装 `~/.local/bin`，官方也会认 | 同 |
| 权限/沙箱 | 直接读写 SQLite 与曲库文件夹 | 走 Tauri 命令，更规整，可被 CSP/scope 约束 |

## 八、几个容易踩的点（读代码得到）

1. **asset 协议 scope 只在启动时注册**：新增曲库文件夹后必须重启 App，否则 `asset://` 播不了。
   我们的下载目录落在 `~/Music/HomeKaraoke`（属于 `$AUDIO`），刚好在默认 scope 内，所以能播。
2. **CSP**：`media-src` 只放行 `self https: blob: asset:`，本地文件必须走 `convertFileSrc`。
3. **机器人校验**：yt-dlp 直接下 YouTube 常触发 “Sign in to confirm you're not a bot”，
   原生实现也得给 `--cookies-from-browser` 留参数，否则用户会以为是 bug。
4. **`download_queue.youtube_id` 是 UNIQUE**：我们的外挂也在写这张表，如果将来上原生下载，
   两边写同一行会冲突，迁移时要注意清历史行。
5. **0.9.0 → 0.9.1 的库结构差异**：你的 `karaoke.db` 已经有 13 个迁移的等价结构，
   迁移 14 必须是「只加列」的幂等写法（`ALTER TABLE ... ADD COLUMN` 需要先探测列是否存在）。

---

## 附：值得注意的质量细节

- `ytdlp.rs` 里的 `find_executable_in_path` 会额外搜索 `~/.local/bin`、`/opt/homebrew/bin`、
  `/usr/local/bin`，并重建 `PATH`（`get_expanded_path`）——这就是为什么把 yt-dlp 丢进
  `~/.local/bin` 后，官方 App 也能识别它。
- `library_scanner` 支持 `.hkmeta.json` 旁挂元数据、`.lrc` 歌词、ffprobe 读时长/年份，
  扫描结果是「文件名 + 旁挂文件」双来源，所以下载器只要把文件命名对，剩下的元数据让 App 补。
- 命令层几乎所有函数都是「打日志 → 调 service → 返回结构化错误」，没有业务逻辑，
  意味着新增下载命令不需要碰既有逻辑，风险面很小。
