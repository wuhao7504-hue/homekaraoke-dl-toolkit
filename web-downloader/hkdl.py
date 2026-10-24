#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HomeKaraoke 下载器 (hkdl)
=========================
给 HomeKaraoke 补上下载能力：搜索 YouTube → 下载到本地曲库文件夹 → 写回
HomeKaraoke 自己的数据库（videos / library_folders / download_queue），
打开 App 的「Local 曲库」即可离线点歌。

依赖：yt-dlp（必需）、ffmpeg/ffprobe（可选，用于 1080p 合并封面/元数据）
两者都会被自动在 ~/.local/bin、/opt/homebrew/bin、/usr/local/bin、PATH 中查找。

GUI:   python3 hkdl.py
CLI:   python3 hkdl.py --url <URL或VIDEO_ID> [--title T] [--artist A] [--audio]
       python3 hkdl.py --search "后来 karaoke"
       python3 hkdl.py --doctor            # 环境自检
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import queue as qmod
import time
import urllib.parse
import urllib.request

APP_TITLE = "HomeKaraoke 下载器"
HK_DB = os.path.expanduser("~/Library/Application Support/app.homekaraoke/karaoke.db")
CFG_DIR = os.path.expanduser("~/Library/Application Support/homekaoke-downloader")
CFG_PATH = os.path.join(CFG_DIR, "config.json")
DEFAULT_DIR = os.path.expanduser("~/Music/HomeKaraoke")

BIN_DIRS = [
    os.path.expanduser("~/.local/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
]

VIDEO_FORMAT = (
    "bv*[ext=mp4][vcodec^=avc1][height<=1080]+ba[ext=m4a]/"
    "bv*[height<=1080]+ba/b[ext=mp4]/b"
)
VIDEO_FORMAT_720 = (
    "bv*[ext=mp4][vcodec^=avc1][height<=720]+ba[ext=m4a]/"
    "b[ext=mp4][height<=720]/b[ext=mp4]/b"
)

SAFE_RE = re.compile(r'[/\\:\*\?"<>\|\x00-\x1f]')


# ---------------------------------------------------------------- utilities
def find_bin(name):
    for d in BIN_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which(name)


def log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        pass


def safe_name(text, limit=120):
    name = SAFE_RE.sub("_", (text or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit] or "karaoke"


def clean_artist(name):
    a = (name or "").strip()
    a = re.sub(r"\s*-\s*Topic$", "", a, flags=re.I)
    a = re.sub(r"\s*(Official\s*)?(VEVO|Music)$", "", a, flags=re.I)
    return a.strip()


def hms(seconds):
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    return "%d:%02d" % (s // 60, s % 60)


def load_cfg():
    cfg = {"dir": DEFAULT_DIR, "format": "视频 MP4 (最高1080p)", "cookies": "无"}
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    os.makedirs(CFG_DIR, exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def db_connect():
    con = sqlite3.connect(HK_DB, timeout=20)
    con.execute("PRAGMA busy_timeout = 20000")
    return con


def app_setting(key, default=None):
    try:
        con = db_connect()
        row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        con.close()
        return row[0] if row else default
    except Exception:
        return default


# ---------------------------------------------------------------- searching
def ytdlp_search(query, limit=25):
    yt = find_bin("yt-dlp")
    if not yt:
        raise RuntimeError("找不到 yt-dlp")
    cmd = [yt, "ytsearch%d:%s" % (limit, query), "--flat-playlist",
           "--dump-json", "--no-warnings", "--ignore-errors"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        items.append({
            "id": d.get("id", ""),
            "title": d.get("title") or "",
            "artist": clean_artist(d.get("channel") or d.get("uploader") or ""),
            "duration": d.get("duration"),
            "source": "yt-dlp",
        })
    return items


def api_search(query, limit=25):
    key = app_setting("youtube_api_key")
    if not key:
        raise RuntimeError("HomeKaraoke 里没有保存 YouTube API Key")
    params = urllib.parse.urlencode({
        "part": "snippet", "type": "video", "videoCategoryId": "10",
        "maxResults": str(min(limit, 50)), "q": query, "key": key,
    })
    url = "https://www.googleapis.com/youtube/v3/search?" + params
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    items, ids = [], []
    for it in data.get("items", []):
        vid = it.get("id", {}).get("videoId")
        if not vid:
            continue
        sn = it.get("snippet", {})
        ids.append(vid)
        items.append({
            "id": vid,
            "title": sn.get("title") or "",
            "artist": clean_artist(sn.get("channelTitle") or ""),
            "duration": None,
            "source": "API",
        })
    if ids:
        try:
            p2 = urllib.parse.urlencode({
                "part": "contentDetails", "id": ",".join(ids), "key": key,
            })
            with urllib.request.urlopen(
                "https://www.googleapis.com/youtube/v3/videos?" + p2, timeout=30
            ) as r:
                vids = json.loads(r.read().decode("utf-8", "replace")).get("items", [])
            dur = {}
            for v in vids:
                iso = v.get("contentDetails", {}).get("duration", "")
                m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
                if m:
                    h, mi, se = (int(x or 0) for x in m.groups())
                    dur[v.get("id")] = h * 3600 + mi * 60 + se
            for it in items:
                it["duration"] = dur.get(it["id"])
        except Exception:
            pass
    return items


def library_wishlist(limit=500):
    """把 HomeKaraoke 里的收藏 / 排歌记录读成下载清单。"""
    try:
        con = db_connect()
        rows = con.execute(
            "SELECT youtube_id, title, artist, duration, '排歌记录' FROM queue_items "
            "WHERE youtube_id IS NOT NULL AND youtube_id <> '' "
            "UNION "
            "SELECT youtube_id, title, artist, duration, '收藏' FROM singer_favorites "
            "WHERE youtube_id IS NOT NULL AND youtube_id <> ''"
        ).fetchall()
        con.close()
    except Exception as exc:
        raise RuntimeError("读取 HomeKaraoke 数据库失败：%s" % exc)
    seen, out = set(), []
    for vid, title, artist, dur, src in rows:
        if vid in seen:
            continue
        seen.add(vid)
        out.append({"id": vid, "title": title or vid, "artist": artist or "",
                    "duration": dur, "src": src})
    return out[:limit]


def local_video_ids():
    """已经下载过的 YouTube id，避免重复下。"""
    try:
        con = db_connect()
        rows = con.execute(
            "SELECT youtube_id FROM videos WHERE source_type = 'local' "
            "AND file_path IS NOT NULL AND youtube_id IS NOT NULL"
        ).fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def search(query, method="自动", limit=25):
    if method in ("自动", "API"):
        try:
            return api_search(query, limit)
        except Exception as exc:
            if method == "API":
                raise
            log("API 搜索失败，改用 yt-dlp：%s" % exc)
    return ytdlp_search(query, limit)


# ---------------------------------------------------------------- downloading
def build_download_cmd(url, out_dir, title, fmt_choice, cookies="无"):
    yt = find_bin("yt-dlp")
    ffmpeg = find_bin("ffmpeg")
    stem = safe_name(title)
    if fmt_choice.startswith("仅音频"):
        args = ["-f", "ba[ext=m4a]/ba", "-x", "--audio-format", "m4a"]
    elif "720" in fmt_choice:
        args = ["-f", VIDEO_FORMAT_720, "--merge-output-format", "mp4"]
    else:
        args = ["-f", VIDEO_FORMAT, "--merge-output-format", "mp4"]

    cmd = [yt, "--no-playlist", "--newline", "--no-warnings",
           "--retries", "5", "--fragment-retries", "5",
           "--concurrent-fragments", "4",
           "--progress-template",
           "download:HKDL|%(progress.downloaded_bytes)s|%(progress.total_bytes_estimate)s|"
           "%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
           "-o", os.path.join(out_dir, stem + ".%(ext)s")] + args

    if ffmpeg:
        cmd += ["--ffmpeg-location", os.path.dirname(ffmpeg)]
        if not fmt_choice.startswith("仅音频"):
            cmd += ["--embed-metadata", "--embed-thumbnail"]
    if cookies and cookies != "无":
        cmd += ["--cookies-from-browser", cookies.lower()]
    cmd.append(url)
    return cmd


def find_output(out_dir, title, since):
    """找到这次下载产出的文件；已存在（yt-dlp 跳过重复下载）也算成功。"""
    stem = safe_name(title)
    cands = []
    try:
        names = os.listdir(out_dir)
    except FileNotFoundError:
        return None
    for name in names:
        if not name.startswith(stem):
            continue
        if name.endswith(".part") or name.endswith(".ytdl") or \
           name.endswith(".webp") or name.endswith(".png") or name.endswith(".jpg"):
            continue
        if not os.path.splitext(name)[1].lower() in (".mp4", ".mkv", ".webm", ".mov",
                                                     ".m4a", ".mp3", ".opus", ".flac"):
            continue
        path = os.path.join(out_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size <= 0:
            continue
        cands.append((st.st_mtime, path))
    if not cands:
        return None
    cands.sort()
    for mtime, path in reversed(cands):
        if mtime >= since - 5:
            return path
    return cands[-1][1]


def register_in_library(path, video_id, title, artist, duration, folder):
    """把下载好的文件登记到 HomeKaraoke 自己的数据库。返回是否新登记了曲库文件夹。"""
    con = db_connect()
    new_folder = False
    try:
        new_folder = con.execute(
            "SELECT 1 FROM library_folders WHERE path = ?", (folder,)).fetchone() is None
        con.execute(
            "INSERT OR IGNORE INTO library_folders (path, name) VALUES (?, ?)",
            (folder, os.path.basename(folder.rstrip("/")) or folder),
        )
        thumb = ("https://i.ytimg.com/vi/%s/hqdefault.jpg" % video_id) if video_id else None
        row = con.execute("SELECT id FROM videos WHERE file_path = ?", (path,)).fetchone()
        if row is None and video_id:
            row = con.execute("SELECT id FROM videos WHERE youtube_id = ?", (video_id,)).fetchone()
        if row:
            con.execute(
                "UPDATE videos SET source_type='local', file_path=?, title=?, "
                "artist=COALESCE(NULLIF(?,''), artist), "
                "duration_seconds=COALESCE(?, duration_seconds), "
                "thumbnail_url=COALESCE(thumbnail_url, ?) WHERE id=?",
                (path, title, artist or "", duration, thumb, row[0]),
            )
        else:
            con.execute(
                "INSERT INTO videos (youtube_id, title, artist, duration_seconds, "
                "source_type, file_path, thumbnail_url) VALUES (?,?,?,?,'local',?,?)",
                (video_id, title, artist or None, duration, path, thumb),
            )
        if video_id:
            con.execute(
                "INSERT INTO download_queue (youtube_id, youtube_title, status, "
                "progress_percent, completed_at) VALUES (?,?, 'completed', 100, CURRENT_TIMESTAMP) "
                "ON CONFLICT(youtube_id) DO UPDATE SET status='completed', "
                "progress_percent=100, completed_at=CURRENT_TIMESTAMP, error_message=NULL",
                (video_id, title),
            )
        con.commit()
    finally:
        con.close()
    return new_folder


def download_one(task, out_dir, fmt_choice, cookies="无", on_progress=None):
    url = task.get("url") or ("https://www.youtube.com/watch?v=" + task["id"])
    started = time.time()
    cmd = build_download_cmd(url, out_dir, task["title"], fmt_choice, cookies)
    log("$ " + " ".join(cmd[:8]) + " ...")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    tail = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("HKDL|"):
            parts = line.split("|")
            if len(parts) >= 6 and on_progress:
                pct = None
                try:
                    pct = float(parts[3].strip().rstrip("%"))
                except ValueError:
                    pct = None
                if pct is None:
                    try:
                        done = float(parts[1] or 0)
                        total = float(parts[2] or 0)
                        pct = done * 100.0 / total if total else 0.0
                    except Exception:
                        pct = 0.0
                on_progress(pct, parts[4].strip(), parts[5].strip())
            continue
        if line.strip():
            tail.append(line)
            tail = tail[-12:]
    proc.wait()
    detail = " | ".join(tail[-4:])[-500:]
    if proc.returncode != 0:
        raise RuntimeError(detail or ("yt-dlp 退出码 %d" % proc.returncode))
    path = find_output(out_dir, task["title"], started)
    if not path:
        raise RuntimeError("yt-dlp 结束但没有产出文件：%s" % detail)
    new_folder = register_in_library(path, task.get("id"), task["title"],
                                     task.get("artist"), task.get("duration"), out_dir)
    return path, new_folder


# ---------------------------------------------------------------- doctor
def doctor():
    yt, ff, fp = find_bin("yt-dlp"), find_bin("ffmpeg"), find_bin("ffprobe")
    print("yt-dlp :", yt or "缺失")
    if yt:
        print("         版本", subprocess.run([yt, "--version"], capture_output=True,
                                            text=True).stdout.strip())
    print("ffmpeg :", ff or "缺失（1080p 合并/封面不可用）")
    print("ffprobe:", fp or "缺失（App 扫库时长识别不可用）")
    print("数据库 :", HK_DB, "存在" if os.path.exists(HK_DB) else "缺失")
    print("曲库目录:", load_cfg()["dir"])
    if os.path.exists(HK_DB):
        con = db_connect()
        print("曲库文件夹:", con.execute("SELECT path FROM library_folders").fetchall())
        print("已登记视频:", con.execute("SELECT COUNT(*) FROM videos").fetchone()[0])
        con.close()


# ---------------------------------------------------------------- GUI
def run_gui(auto_test=None):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    BG = "#111827"
    CARD = "#1f2937"
    TXT = "#e5e7eb"
    MUT = "#9ca3af"
    ACC = "#6366f1"
    LOGPATH = os.path.expanduser("~/Library/Logs/hkdl.log")

    def file_log(msg):
        try:
            os.makedirs(os.path.dirname(LOGPATH), exist_ok=True)
            with open(LOGPATH, "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    file_log("---- 启动，python %s，tk %s" % (sys.version.split()[0], "ok"))

    cfg = load_cfg()
    out_q = qmod.Queue()                      # 工作线程 → 主线程 的唯一通道
    state = {"results": [], "queue": [], "running": False, "stop": False}
    auto = {"phase": "search", "query": auto_test} if auto_test else None

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1020x780")
    root.minsize(860, 640)
    root.configure(bg=BG)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TXT, font=("PingFang SC", 12))
    style.configure("Muted.TLabel", background=BG, foreground=MUT, font=("PingFang SC", 11))
    style.configure("H.TLabel", background=BG, foreground=TXT, font=("PingFang SC", 15, "bold"))
    style.configure("TButton", font=("PingFang SC", 12), padding=6)
    style.configure("Accent.TButton", font=("PingFang SC", 12, "bold"), padding=6)
    style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TXT,
                    rowheight=26, font=("PingFang SC", 11), borderwidth=0)
    style.configure("Treeview.Heading", background="#374151", foreground=TXT,
                    font=("PingFang SC", 11, "bold"))
    style.map("Treeview", background=[("selected", ACC)])
    style.configure("TEntry", fieldbackground=CARD, foreground=TXT, insertcolor=TXT)
    style.configure("TCombobox", fieldbackground=CARD, foreground=TXT)

    # ---------- header
    head = ttk.Frame(root)
    head.pack(fill="x", padx=16, pady=(14, 6))
    ttk.Label(head, text="HomeKaraoke 下载器", style="H.TLabel").pack(side="left")
    ttk.Label(head, text="搜索 → 下载 → 自动进 Local 曲库",
              style="Muted.TLabel").pack(side="left", padx=12)

    # ---------- search row
    srow = ttk.Frame(root)
    srow.pack(fill="x", padx=16, pady=6)
    qvar = tk.StringVar()
    entry = ttk.Entry(srow, textvariable=qvar, font=("PingFang SC", 13))
    entry.pack(side="left", fill="x", expand=True, ipady=5)
    mvar = tk.StringVar(value="自动")
    ttk.Combobox(srow, textvariable=mvar, values=["自动", "API", "yt-dlp"],
                 width=8, state="readonly").pack(side="left", padx=8)
    btn_search = ttk.Button(srow, text="搜索", style="Accent.TButton", width=8)
    btn_search.pack(side="left")

    status = tk.StringVar(value="就绪")
    ttk.Label(root, textvariable=status, style="Muted.TLabel").pack(fill="x", padx=18)

    # ---------- results
    rwrap = ttk.Frame(root)
    rwrap.pack(fill="both", expand=True, padx=16, pady=(4, 8))
    rtree = ttk.Treeview(rwrap, columns=("title", "artist", "dur", "src"),
                         show="headings", selectmode="extended", height=7)
    for c, t, w, a in (("title", "歌曲标题", 540, "w"), ("artist", "频道 / 歌手", 250, "w"),
                       ("dur", "时长", 80, "center"), ("src", "来源", 70, "center")):
        rtree.heading(c, text=t)
        rtree.column(c, width=w, anchor=a, stretch=(c == "title"))
    rtree.pack(side="left", fill="both", expand=True)
    rsb = ttk.Scrollbar(rwrap, orient="vertical", command=rtree.yview)
    rsb.pack(side="right", fill="y")
    rtree.configure(yscrollcommand=rsb.set)

    # ---------- queue actions
    arow = ttk.Frame(root)
    arow.pack(fill="x", padx=16)
    ttk.Button(arow, text="↓ 加入下载队列", command=lambda: add_selected()).pack(side="left")
    ttk.Button(arow, text="导入收藏 / 排歌记录",
               command=lambda: import_wishlist()).pack(side="left", padx=8)
    ttk.Button(arow, text="清空", command=lambda: clear_all()).pack(side="left")
    ttk.Button(arow, text="自检", command=lambda: self_check()).pack(side="left", padx=8)
    btn_start = ttk.Button(arow, text="开始下载", style="Accent.TButton")
    btn_start.pack(side="right")
    btn_stop = ttk.Button(arow, text="停止", state="disabled")
    btn_stop.pack(side="right", padx=8)
    ttk.Button(arow, text="打开文件夹", command=lambda: open_dir()).pack(side="right", padx=8)
    ttk.Button(arow, text="打开 HomeKaraoke",
               command=lambda: subprocess.run(["open", "-a", "HomeKaraoke"])
               ).pack(side="right", padx=8)
    ttk.Button(arow, text="重启 HomeKaraoke",
               command=lambda: restart_hk()).pack(side="right", padx=8)

    # ---------- queue
    qwrap = ttk.Frame(root)
    qwrap.pack(fill="both", expand=True, padx=16, pady=(8, 4))
    qtree = ttk.Treeview(qwrap, columns=("song", "state", "pct", "speed"),
                         show="headings", height=6)
    for c, t, w, a in (("song", "队列", 540, "w"), ("state", "状态", 150, "w"),
                       ("pct", "进度", 90, "center"), ("speed", "速度 / 剩余", 160, "center")):
        qtree.heading(c, text=t)
        qtree.column(c, width=w, anchor=a, stretch=(c == "song"))
    qtree.pack(side="left", fill="both", expand=True)
    qsb = ttk.Scrollbar(qwrap, orient="vertical", command=qtree.yview)
    qsb.pack(side="right", fill="y")
    qtree.configure(yscrollcommand=qsb.set)

    # ---------- log pane
    lwrap = ttk.Frame(root)
    lwrap.pack(fill="x", padx=16, pady=(4, 6))
    logbox = tk.Text(lwrap, height=4, bg="#0b1220", fg=MUT, bd=0,
                     font=("Menlo", 10), insertbackground=TXT)
    logbox.pack(side="left", fill="x", expand=True)
    logbox.configure(state="disabled")

    def ui_log(msg):
        file_log(msg)
        def apply():
            logbox.configure(state="normal")
            logbox.insert("end", msg + "\n")
            if int(logbox.index("end-1c").split(".")[0]) > 400:
                logbox.delete("1.0", "100.0")
            logbox.see("end")
            logbox.configure(state="disabled")
        try:
            root.after(0, apply)
        except Exception:
            pass

    # ---------- footer
    frow = ttk.Frame(root)
    frow.pack(fill="x", padx=16, pady=(0, 12))
    ttk.Label(frow, text="保存到：", style="Muted.TLabel").pack(side="left")
    dvar = tk.StringVar(value=cfg["dir"])
    ttk.Entry(frow, textvariable=dvar, width=44).pack(side="left", ipady=3)
    ttk.Button(frow, text="选择…", command=lambda: pick_dir()).pack(side="left", padx=6)
    fvar = tk.StringVar(value=cfg.get("format", "视频 MP4 (最高1080p)"))
    ttk.Combobox(frow, textvariable=fvar, width=22, state="readonly",
                 values=["视频 MP4 (最高1080p)", "视频 MP4 (720p)", "仅音频 M4A"]
                 ).pack(side="left", padx=(14, 0))
    cvar = tk.StringVar(value=cfg.get("cookies", "无"))
    ttk.Combobox(frow, textvariable=cvar, width=12, state="readonly",
                 values=["无", "Safari", "Chrome", "Edge", "Firefox"]
                 ).pack(side="left", padx=8)
    ttk.Label(frow, text="Cookie", style="Muted.TLabel").pack(side="left")

    # ================= 主线程动作（所有 Tk 调用只在这里发生） =================
    def set_status(text):
        status.set(text)

    def clear_all():
        rtree.delete(*rtree.get_children())
        qtree.delete(*qtree.get_children())
        state["results"].clear()
        state["queue"].clear()
        set_status("已清空")

    def queue_row(task):
        tid = "t%d_%d" % (int(time.time() * 1000), len(state["queue"]))
        task["tid"] = tid
        task["status"] = "等待中"
        state["queue"].append(task)
        qtree.insert("", "end", iid=tid, values=(task["title"], "等待中", "0%", ""))

    def add_selected():
        sel = rtree.selection()
        if not sel:
            set_status("先在搜索结果里选中歌曲（可按住 ⌘ 多选）")
            return
        for iid in sel:
            it = state["results"][int(iid)]
            queue_row({"id": it["id"], "title": it["title"], "artist": it.get("artist"),
                       "duration": it.get("duration"),
                       "url": "https://www.youtube.com/watch?v=" + it["id"]})
        set_status("已加入 %d 首，点「开始下载」" % len(sel))
        ui_log("入队 %d 首" % len(sel))

    def import_wishlist():
        try:
            items = library_wishlist()
        except Exception as exc:
            set_status("读取失败：%s" % exc)
            ui_log("导入失败：%s" % exc)
            return
        if not items:
            set_status("HomeKaraoke 里还没有收藏或排歌记录")
            return
        have = local_video_ids()
        added = 0
        for it in items:
            if it["id"] in have:
                continue
            queue_row({"id": it["id"], "title": it["title"], "artist": it["artist"],
                       "duration": it["duration"],
                       "url": "https://www.youtube.com/watch?v=" + it["id"]})
            added += 1
        msg = ("从 HomeKaraoke 导入 %d 首（跳过本地已有 %d 首），点「开始下载」"
               % (added, len(items) - added)) if added else \
              ("收藏 / 排歌记录里的 %d 首都下载过了" % len(items))
        set_status(msg)
        ui_log(msg)

    def pick_dir():
        d = filedialog.askdirectory(initialdir=dvar.get() or DEFAULT_DIR)
        if d:
            dvar.set(d)
            cfg["dir"] = d
            save_cfg(cfg)
            ui_log("曲库目录改为 %s" % d)

    def open_dir():
        d = dvar.get()
        os.makedirs(d, exist_ok=True)
        subprocess.run(["open", d])

    def self_check():
        def work():
            yt, ff, fp = find_bin("yt-dlp"), find_bin("ffmpeg"), find_bin("ffprobe")
            out_q.put(("log", "yt-dlp=%s ffmpeg=%s ffprobe=%s" % (yt, ff, fp)))
            try:
                con = db_connect()
                n_local = con.execute(
                    "SELECT COUNT(*) FROM videos WHERE source_type='local'").fetchone()[0]
                folders = [r[0] for r in con.execute("SELECT path FROM library_folders")]
                con.close()
                out_q.put(("log", "数据库可写 · 本地曲目 %d 首 · 曲库文件夹 %s"
                           % (n_local, folders)))
            except Exception as exc:
                out_q.put(("log", "数据库异常：%s" % exc))
            d = dvar.get()
            out_q.put(("log", "目标目录 %s %s" % (d, "可写" if os.access(d, os.W_OK)
                                                 else "不可写（用「选择…」换一个）")))
            try:
                items = search("karaoke", "yt-dlp", 3)
                out_q.put(("log", "YouTube 连通正常（返回 %d 条）" % len(items)))
            except Exception as exc:
                out_q.put(("log", "YouTube 搜索失败：%s" % exc))
        threading.Thread(target=work, daemon=True).start()

    def restart_hk():
        """重启 HomeKaraoke：让它重新注册本地文件的 asset 协议范围。"""
        set_status("正在重启 HomeKaraoke…")
        subprocess.run(["osascript", "-e", 'tell application "HomeKaraoke" to quit'],
                       capture_output=True)
        time.sleep(2)
        subprocess.run(["open", "-a", "HomeKaraoke"])
        set_status("HomeKaraoke 已重启 → 进 Local 点「扫描」")
        ui_log("已重启 HomeKaraoke")

    def do_search():
        q = qvar.get().strip()
        if not q:
            set_status("先输入歌名")
            return
        method = mvar.get()                     # 主线程取值
        btn_search.configure(state="disabled")
        set_status("搜索中…")
        ui_log("搜索「%s」(%s)" % (q, method))

        def work():
            try:
                items = search(q, method)
                out_q.put(("results", items))
            except Exception as exc:
                out_q.put(("search_failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def start_run():
        if state["running"]:
            return
        pending = [t for t in state["queue"] if t.get("status") in (None, "等待中")]
        if not pending:
            set_status("队列是空的：先搜歌加入队列")
            return
        # 主线程一次性取走所有 Tk 变量，工作线程不再碰 Tk
        out_dir = dvar.get() or DEFAULT_DIR
        fmt = fvar.get()
        cookies = cvar.get()
        os.makedirs(out_dir, exist_ok=True)
        cfg.update({"dir": out_dir, "format": fmt, "cookies": cookies})
        save_cfg(cfg)

        state["running"] = True
        state["stop"] = False
        btn_start.configure(state="disabled")
        btn_stop.configure(state="normal")
        set_status("开始下载 %d 首…" % len(pending))
        ui_log("开始下载 %d 首 → %s (%s)" % (len(pending), out_dir, fmt))

        def work():
            for task in pending:
                if state["stop"]:
                    out_q.put(("row", (task["tid"], (task["title"], "已取消", "—", ""),)))
                    continue
                out_q.put(("row", (task["tid"], (task["title"], "下载中…", "0%", ""),)))
                last = [0.0]

                def on_prog(pct, speed, eta, tid=task["tid"], title=task["title"]):
                    now = time.time()
                    if now - last[0] < 0.4:
                        return
                    last[0] = now
                    info = (speed or "").strip()
                    if eta and eta not in ("NA", "Unknown"):
                        info = "%s / %s" % (info, eta.strip())
                    out_q.put(("row", (tid, (title, "下载中…", "%.0f%%" % pct, info))))

                try:
                    path, new_folder = download_one(task, out_dir, fmt, cookies, on_prog)
                    size = os.path.getsize(path) / 1048576.0
                    out_q.put(("row", (task["tid"],
                                       (task["title"], "完成 · 已入库", "100%",
                                        "%.0f MB" % size))))
                    out_q.put(("saved", (path, new_folder)))
                except Exception as exc:
                    out_q.put(("row", (task["tid"], (task["title"], "失败", "—", str(exc)[:70]))))
                    out_q.put(("failed", str(exc)))
            out_q.put(("all_done", None))

        threading.Thread(target=work, daemon=True).start()

    def stop_run():
        state["stop"] = True
        set_status("正在停止…")

    def fill_results(items):
        rtree.delete(*rtree.get_children())
        state["results"] = items
        for idx, it in enumerate(items):
            rtree.insert("", "end", iid=str(idx),
                         values=(it["title"], it["artist"], hms(it["duration"]),
                                 it.get("source", "")))
        set_status("找到 %d 个结果：选中后点「加入下载队列」" % len(items))
        ui_log("搜索结果 %d 条" % len(items))

    def finish_run():
        state["running"] = False
        btn_start.configure(state="normal")
        btn_stop.configure(state="disabled")
        done = len([t for t in state["queue"] if t.get("status") == "done_ok"]) or \
            len([r for r in qtree.get_children() if "已入库" in qtree.item(r, "values")[1]])
        if done:
            set_status("%d 首已入库 · 打开 HomeKaraoke → Local → 扫描" % done)

    # ================= 事件泵：把工作线程的消息在主线程执行 =================
    def drain():
        try:
            while True:
                kind, payload = out_q.get_nowait()
                if kind == "results":
                    fill_results(payload)
                    if auto and auto.get("phase") == "search":
                        auto["phase"] = "download"
                        if payload:
                            rtree.selection_set(str(0))
                            add_selected()
                            root.after(300, start_run)
                        else:
                            file_log("AUTOTEST: 搜索无结果"); root.after(200, root.destroy)
                elif kind == "search_failed":
                    set_status("搜索失败：%s" % payload)
                    ui_log("搜索失败：%s" % payload)
                    if auto:
                        file_log("AUTOTEST: 搜索失败 %s" % payload)
                        root.after(200, root.destroy)
                    else:
                        messagebox.showerror(APP_TITLE, "搜索失败：\n%s" % payload)
                elif kind == "row":
                    tid, values = payload
                    if qtree.exists(tid):
                        qtree.item(tid, values=values)
                elif kind == "saved":
                    path, new_folder = payload
                    for t in state["queue"]:
                        if t.get("status") != "取消":
                            t["status"] = "done_ok"
                    if new_folder:
                        set_status("完成：%s · 新增曲库文件夹，重启 HomeKaraoke 后生效"
                                   % os.path.basename(path))
                    file_log("SAVED %s | new_folder=%s" % (path, new_folder))
                    ui_log("已入库 %s" % os.path.basename(path))
                elif kind == "log":
                    ui_log(payload)
                elif kind == "failed":
                    file_log("FAILED %s" % payload)
                    set_status("失败：%s" % payload[:160])
                elif kind == "all_done":
                    finish_run()
                    if auto and auto.get("phase") == "download":
                        file_log("AUTOTEST: 完成")
                        root.after(400, root.destroy)
        except qmod.Empty:
            pass
        except Exception as exc:                  # 泵本身出错也要留痕
            file_log("DRAIN ERROR %r" % exc)
        root.after(120, drain)

    btn_search.configure(command=do_search)
    entry.bind("<Return>", lambda e: do_search())
    btn_start.configure(command=start_run)
    btn_stop.configure(command=stop_run)
    rtree.bind("<Double-1>", lambda e: add_selected())

    ui_log("yt-dlp: %s · ffmpeg: %s · 曲库: %s" % (
        find_bin("yt-dlp") or "缺失", find_bin("ffmpeg") or "缺失", cfg["dir"]))
    root.after(120, drain)

    def probe_window():
        try:
            root.update_idletasks()
            file_log("窗口 %dx%d @%d,%d mapped=%s viewable=%s" % (
                root.winfo_width(), root.winfo_height(), root.winfo_rootx(),
                root.winfo_rooty(), root.winfo_ismapped(), root.winfo_viewable()))
        except Exception as exc:
            file_log("窗口探测失败 %r" % exc)
    root.after(900, probe_window)

    if auto:
        qvar.set(auto["query"])
        root.after(600, do_search)

    try:
        root.mainloop()
    except Exception:
        import traceback
        file_log("MAINLOOP CRASH:\n" + traceback.format_exc())
        raise
    file_log("---- 退出")


# ---------------------------------------------------------------- CLI
def main(argv):
    if "--doctor" in argv:
        doctor()
        return 0
    if "--search" in argv:
        q = argv[argv.index("--search") + 1]
        for it in ytdlp_search(q):
            print("%s\t%s\t%s\t%s" % (it["id"], hms(it["duration"]), it["artist"], it["title"]))
        return 0
    if "--url" in argv:
        target = argv[argv.index("--url") + 1]
        vid = target
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", target)
        if m:
            vid = m.group(1)
        title = argv[argv.index("--title") + 1] if "--title" in argv else vid
        artist = argv[argv.index("--artist") + 1] if "--artist" in argv else ""
        cfg = load_cfg()
        fmt = "仅音频 M4A" if "--audio" in argv else cfg["format"]
        out = argv[argv.index("--dir") + 1] if "--dir" in argv else cfg["dir"]
        os.makedirs(out, exist_ok=True)
        task = {"id": vid, "title": title, "artist": artist, "duration": None,
                "url": "https://www.youtube.com/watch?v=" + vid}
        path, new_folder = download_one(
            task, out, fmt, cfg.get("cookies", "无"),
            lambda p, s, e: print("  %5.1f%% %s" % (p, s), end="\r", flush=True))
        print("\n完成:", path)
        if new_folder:
            print("提示：新增了曲库文件夹，重启 HomeKaraoke 后生效")
        return 0
    if "--gui-test" in argv:
        q = argv[argv.index("--gui-test") + 1]
        run_gui(auto_test=q)
        return 0
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
