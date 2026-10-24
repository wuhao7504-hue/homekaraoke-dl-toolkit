# 🎤 HomeKaraoke —— 免费开源的「家庭点歌机」

**中文** ｜ [English below](#-english)

一款免费、开源的家庭 KTV 点歌机。歌单来自 YouTube，所以**不用买歌、不用买曲库**，
想唱什么搜什么，伴奏和 MV 直接在电视或电脑上放。适合家里、聚会、小店。

> A free, open-source home karaoke machine. Songs come from YouTube — no song library to buy.
> Search anything, play the music video on your TV or laptop. Made for home parties.

---

## ✨ 它能做什么 / What it does

| 功能 | 说明 |
|---|---|
| 搜歌即唱 | 输入歌名，直接从 YouTube 取 MV / 伴奏播放 |
| 多人点歌 | 排队、切歌、按歌手分组，支持双屏（一个屏幕唱歌、一个屏幕点歌） |
| 下载到本地 | 把喜欢的歌存到电脑里，之后**不联网也能唱**，起播零等待 |
| 自动预缓存 | 唱这首歌时，自动把后面 3 首提前下载好 |
| 本地曲库 | 支持扫描自己的视频文件夹，自动生成封面、歌词、`.hkmeta` 信息 |
| 登录同步 | 用 Google / Apple / 邮箱登录，可保存收藏与歌单 |

English: search & sing instantly from YouTube · multi-singer queue · dual-screen · download for
offline singing · auto-prefetch next 3 songs · local library with covers & lyrics · optional sign-in.

---

## 📦 这个 App 从哪来 / Where it comes from

- **原版作者 / Upstream**：Piotr Zalewa — [github.com/zalun/karaoke-app](https://github.com/zalun/karaoke-app)
- **官网 / Website**：[homekaraoke.app](https://homekaraoke.app)
- **开源协议 / License**：MIT（可自由使用、修改、分发）
- **本仓库基于**：v0.9.1（Tauri 2 + React 18 + Rust + SQLite）

原版是**完全免费的开源软件**，本仓库不是它的官方版本，是在它基础上增加功能的个人构建。

> The original app is free & open source (MIT). This repository is an unofficial build with
> extra features, not affiliated with the upstream author.

---

## 🔧 我做了哪些工作 / What I built here

原版**没有下载功能**（仓库里只有一份标注 *Planned* 的设计文档）。以下都是在本仓库里完成的：

| # | 工作 | 说明 |
|---|---|---|
| 1 | **原生下载功能** | 新增 Rust 服务与命令，调用 yt-dlp 下载；三个入口都可下：搜索结果行 `⤓`、**队列每行 `⤓`**、**播放器标题旁「⤓ 下载」**；下载完自动入库到「本地曲库」 |
| 2 | **播放时预缓存** | 正在唱这首时，静默下载队列里接下来 3 首；轮到它们时直接从硬盘起播 |
| 3 | **403 分级策略** | 实测 YouTube 对默认客户端的 DASH 流约 5MB 后必 403，因此改为**客户端轮换阶梯**：默认 → `web_safari` → `mweb` → 保底 360p/m4a；每次换策略界面会显示「换策略 2/4」；全部失败才冷却（5 分钟 3 次 → 冷却 15 分钟） |
| 4 | **Cookie 支持** | 支持从 Chrome / Safari / Edge / Firefox 取登录状态，或指定 `cookies.txt`，解决 YouTube 机器人验证 |
| 5 | **本地曲库封面** | 修掉扫描中断 bug（异步里创建 runtime 导致崩溃），下载后自动生成封面缩略图 |
| 6 | **登录修复** | 补齐构建所需的 Supabase 公开配置，修复「登录成功却一直显示未登录」 |
| 7 | **环境隔离** | 应用改用独立端口 14201 与固定自签证书，避免与官方版抢端口、反复弹钥匙串密码 |
| 8 | **外挂下载器** | 另一套不改动原 App 的下载工具（Python + 本地网页界面） |
| 9 | **图标与快捷方式** | 重新绘制麦克风+下载箭头图标，配置桌面 / Dock 快捷方式 |
| 10 | **修上游未通过的测试** | 补丁把 schema 版本推到 14 后，上游测试 `test_schema_version_is_13…` 会失败；已改为 14 并新增迁移 14 的列断言（现在 173 个测试全绿） |
| 11 | **切断官方更新源** | 应用内自动更新的地址原本指向官方 release，会把本构建覆盖回官方版（下载功能消失）；已指向本仓库 |

代码层面：`services/downloader.rs`、`commands/downloads.rs`、数据库迁移 14、
`downloadStore` + `DownloadPanel`、搜索行按钮、曲库封面兜底等，共 18 个文件、约 1300 行新增。
完整补丁见 [`patches/native-download.patch`](patches/native-download.patch)，细节见 [`docs/native-integration.md`](docs/native-integration.md)。

---

## 💻 支持的系统 / Supported systems

| 系统 | 状态 | 说明 |
|---|---|---|
| macOS 12+（Apple 芯片 M1/M2/M3/M4） | ✅ 已构建并提供 | 本仓库 Release 里的包就是这一版（ad-hoc 签名） |
| macOS Intel（x86_64） | 🛠 可自行构建 | `pnpm tauri build --target x86_64-apple-darwin` |
| Windows 10/11 | 🛠 上游支持，需自行构建 | 下载依赖 `yt-dlp.exe` / `ffmpeg.exe` |
| Linux（deb / AppImage） | 🛠 上游支持，需自行构建 | 需自行安装 yt-dlp、ffmpeg |

依赖（三平台相同）：**yt-dlp**（必需，负责下载）、**ffmpeg / ffprobe**（封面与音视频合并）。

---

## 📥 安装手册 / Installation Guide

### 第 1 步：下载
打开仓库右侧 **Releases** → 下载 `HomeKaraoke-DL-0.9.1.zip` → 解压得到 `HomeKaraoke DL.app`。

### 第 2 步：安装
把 `HomeKaraoke DL.app` 拖进「应用程序」文件夹。

### 第 3 步：首次打开（重要）
这个包是**个人签名（ad-hoc）**，macOS 会拦截一次：

1. 双击 App，弹出“无法验证开发者” → 点「好」
2. 打开 **系统设置 → 隐私与安全性**，页面下方点「**仍要打开**」
3. 再双击一次即可正常启动

### 第 4 步：装依赖（只需一次）

打开「终端」，整段粘贴回车：

```bash
mkdir -p ~/.local/bin
# yt-dlp（下载器）
curl -L -o ~/.local/bin/yt-dlp https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos
chmod +x ~/.local/bin/yt-dlp
# ffmpeg + ffprobe（合并音视频、生成封面）
curl -sSL -o /tmp/ffmpeg.zip  https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffmpeg.zip
curl -sSL -o /tmp/ffprobe.zip https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffprobe.zip
unzip -o /tmp/ffmpeg.zip  -d /tmp/ff >/dev/null && unzip -o /tmp/ffprobe.zip -d /tmp/ff >/dev/null
cp /tmp/ff/ffmpeg /tmp/ff/ffprobe ~/.local/bin/ && chmod +x ~/.local/bin/ffmpeg ~/.local/bin/ffprobe
```

检查是否成功（能打印出版本号就对了）：

```bash
~/.local/bin/yt-dlp --version
~/.local/bin/ffmpeg -version | head -1
```

> 已经装了 Homebrew 的话，`brew install yt-dlp ffmpeg` 效果一样。

### 第 5 步：开始用
打开 App → 搜索歌名 → 点结果右侧的 **`⤓`** 下载（想直接唱就点 **▶**）→
下载完成的歌会出现在 **Local（本地）** 标签里，下次不联网也能唱。

> Installation in English: download the zip from **Releases** → drag to Applications → first launch
> goes to **System Settings → Privacy & Security → Open Anyway** → install `yt-dlp` and `ffmpeg`
> as shown above → search a song → click **⤓** to download, **▶** to sing.

---

## 🔑 注册与登录 / Sign up & Sign in

原版支持用 Google / Apple / 邮箱账号登录，用于保存收藏和歌单（不登录也能正常唱歌和下载）。

- 官网 / 注册入口：**https://homekaraoke.app**
- App 内使用的登录页：**https://homekaraoke.app/auth/app-login**

在 App 里点「Sign in」会自动打开浏览器，登录成功后自动跳回 App。
首次登录会请求钥匙串权限，点「始终允许」即可。

> Sign-in is optional. The app opens your browser, you sign in with Google / Apple / email,
> and it comes back automatically.

---

## ❓ 常见问题 / FAQ

**1. 下载时提示 `Sign in to confirm you're not a bot`**
YouTube 认为你在批量抓取。打开下载面板，把 **Cookie** 选成 `Chrome`（先在 Chrome 里登录 YouTube），再点下载。

**2. 提示 `HTTP Error 403: Forbidden`**
两种原因：①短时间大量下载被限流；②YouTube 要求 PO Token，默认客户端的高清流会在几 MB 后 403。
App 会自动换客户端重试（默认 → web_safari → mweb → 保底 360p），多数情况能自愈；
连续 3 次全策略失败才进入 15 分钟冷却。建议一次别超过 5–10 首。

**3. 下载完了，Local 里看不到**
点一下 Local 标签右上角的「扫描」；首次新增曲库文件夹后需要重启一次 App。

**4. 下载的歌存在哪？**
默认 `~/Music/HomeKaraoke`（可在设置里改，也可在设置里指定曲库文件夹）。

**5. 提示“App 已损坏”或打不开**
系统设置 → 隐私与安全性 → 仍要打开。仍不行就在终端执行：
`xattr -cr "/Applications/HomeKaraoke DL.app"`

---

## 🛠 从源码构建 / Build from source

```bash
git clone https://github.com/zalun/karaoke-app.git
cd karaoke-app
git apply /path/to/patches/native-download.patch
# 补上 Supabase 公开配置（官网构建内置值），否则登录不可用
echo 'VITE_SUPABASE_URL=...'      >  .env
echo 'VITE_SUPABASE_ANON_KEY=...' >> .env
pnpm install
pnpm tauri build --bundles app
```

需要：Rust（stable）、Node 20+、pnpm、Xcode 命令行工具。

---

## 📁 仓库结构 / Repository layout

```
patches/      原生下载补丁（git apply 到 upstream）
docs/         上游代码分析、改造说明
web-downloader/  不改动 App 的外挂下载工具（Python + 本地网页）
assets/       App 图标
```

## 📄 许可 / License

MIT。基于 [zalun/karaoke-app](https://github.com/zalun/karaoke-app)（MIT）。
下载内容请自行确认版权与当地法规。

> MIT licensed. Built on top of zalun/karaoke-app (MIT). Please respect copyright and local law
> when downloading content.
