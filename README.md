# HomeKaraoke DL Toolkit

给 [zalun/karaoke-app](https://github.com/zalun/karaoke-app)（HomeKaraoke，Tauri 2 + React）补上
**下载到本地曲库**的能力，并附带一套可直接使用的下载工具。

上游目前只有设计文档（`plan/06-downloads.md` 状态是 *Planned*）和闲置的 `download_queue` 表，
没有实现下载。本仓库提供两条路线：

| 路线 | 目录 | 说明 |
|---|---|---|
| **原生补丁**（推荐） | `patches/native-download.patch` | 直接在 0.9.1 源码上加下载命令 + UI，搜索行点 `⤓` 即可下载并入库 |
| **外挂工具** | `web-downloader/` | 不改动 App：读写它的 SQLite 和曲库目录，本地网页界面操作 |

## 原生补丁做了什么

应用到干净 clone（`zalun/karaoke-app` v0.9.1）：

```bash
git clone https://github.com/zalun/karaoke-app.git
cd karaoke-app
git apply /path/to/patches/native-download.patch
pnpm install
pnpm tauri build --bundles app
```

| 层 | 内容 |
|---|---|
| `services/downloader.rs`（新） | 起 yt-dlp、解析 `[download] 42.3%`、发 `download:progress` 事件、进程登记表（可取消）、精确匹配产出文件、全局单槽位串行 |
| `commands/downloads.rs`（新） | `youtube_download` / `download_cancel` / `download_list` / `download_info` / `download_cached_path` / `download_set_cookies` / `download_set_robust` |
| `db/schema.rs` | 迁移 14：`download_queue` 增 `file_path / quality / file_size_bytes / download_id` |
| 前端 | `downloadStore` + `DownloadPanel`（右下角进度、取消、Cookie 来源、稳健模式、预取开关）、搜索行 `⤓` 按钮、曲库封面 |
| `services/keep_awake.rs`（新） | 用 `/usr/bin/caffeinate` 替换 `keepawake` crate（后者依赖的 `apple-sys` 在新 Xcode 下编译失败） |

### 关键行为

- **下载目录 = 曲库目录**：优先 `settings.download_dir`，否则取 `library_folders` 第一条，最后兜底 `<audio>/HomeKaraoke`
- **稳健模式**（默认开）：`--concurrent-fragments 1 --http-chunk-size 10M --sleep-interval 2 --sleep-requests 1`
- **403 处理**：指数退避 8s → 30s → 120s，每次重跑 yt-dlp（重新签名直链）；5 分钟内 3 次 403 触发**全局冷却 15 分钟**（断路器）
- **预取**：播放时把队列接下来 3 首静默下载（720p），轮到它们时直接从磁盘起播；预取走 `Background` 优先级，永不阻塞用户手动下载
- **正确性**：同一首并发请求合并（in-flight 去重）、产出文件精确匹配、失败也会写 `download_queue`、扫描用 `spawn_blocking` 并在 45 秒内合并

## 下载为何会失败（原理速查）

```
① 解析：watch 页面/player API → 带签名的 googlevideo 直链
② 取流：GET 直链真正拉音视频数据   ← 403 在这一步
```

| 现象 | 原因 | 对策 |
|---|---|---|
| `Sign in to confirm you're not a bot` | ①阶段被拦，缺可信会话 | 登录 Cookie（`--cookies-from-browser`） |
| `HTTP Error 403: Forbidden` | ②阶段：直链过期 / IP 变更 / n-sig 未解 / 缺 PO Token / 并发过高被限流 | 重解析直链、同出口 IP、低并发、断路器 |
| 批量下载后集体失败 | 边缘节点限流 | 串行 + 分段请求 + 请求间隔 |

## 外挂工具（不改 App）

```bash
cd web-downloader
python3 hkdl.py --doctor            # 环境自检
python3 hkapp.py                    # 起本地服务，浏览器开 http://127.0.0.1:18765/
./build_app.sh                      # 可选：打包成 macOS .app
```

依赖：`yt-dlp`（必需）、`ffmpeg`/`ffprobe`（封面与合并）。两者会被自动在
`~/.local/bin`、`/opt/homebrew/bin`、`/usr/local/bin`、`PATH` 中查找。

## 文档

- [`docs/upstream-analysis.md`](docs/upstream-analysis.md)：上游 1.2 万行 Rust + 3.5 万行 TS 的结构、命令面、数据模型、下载缺口定位
- [`docs/native-integration.md`](docs/native-integration.md)：本次改造的逐项改动、验证记录、构建踩坑（`apple-sys` / tauri CLI argv 问题）、Supabase 配置说明

## 许可

MIT。补丁基于 [zalun/karaoke-app](https://github.com/zalun/karaoke-app)（MIT）。
下载内容请自行确认使用权与当地法规。
