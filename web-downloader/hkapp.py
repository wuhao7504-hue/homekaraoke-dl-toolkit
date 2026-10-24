#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HomeKaraoke 下载器 · 本地 Web 界面
=================================
为什么用浏览器窗口：Tk 窗口在 macOS 上会落在别的 Space / 排在后面，经常「看不见」；
本地网页窗口一定能到最前，而且能显示封面、进度条和日志。

后端：127.0.0.1:18765（只监听本机回环，不对外）
用法：python3 hkapp.py            启动服务（前台）
      python3 hkapp.py --open-ui  启动服务并打开窗口
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hkdl  # noqa: E402

PORT = int(os.environ.get("HKDL_PORT", "18765"))
HERE = os.path.dirname(os.path.abspath(__file__))
LOGPATH = os.path.expanduser("~/Library/Logs/hkdl.log")

_cfg = hkdl.load_cfg()
STATE = {
    "dir": _cfg["dir"],
    "format": _cfg.get("format", "视频 MP4 (最高1080p)"),
    "cookies": _cfg.get("cookies", "无"),
    "running": False,
    "stop": False,
    "last_ping": time.time(),
}
TASKS = []
STREAMS = {"active": 0, "last_closed": time.time()}
BUS = {"seq": 0, "items": []}
LOCK = threading.Lock()


def file_log(text):
    try:
        os.makedirs(os.path.dirname(LOGPATH), exist_ok=True)
        with open(LOGPATH, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), text))
    except Exception:
        pass


def emit(kind, **payload):
    with LOCK:
        BUS["seq"] += 1
        ev = {"seq": BUS["seq"], "kind": kind}
        ev.update(payload)
        BUS["items"].append(ev)
        if len(BUS["items"]) > 1200:
            del BUS["items"][:500]
    if kind in ("log", "saved", "failed"):
        file_log("%s %s" % (kind, payload.get("text") or payload.get("path") or payload.get("error") or ""))
    return ev


def task_pub(t):
    return {k: t.get(k) for k in
            ("tid", "id", "title", "artist", "duration", "status", "pct", "speed", "error")}


# ------------------------------------------------------------------ 动作
def do_search(q, method):
    items = hkdl.search(q, method)
    for it in items:
        it["thumb"] = "https://i.ytimg.com/vi/%s/mqdefault.jpg" % it["id"]
        it["dur"] = hkdl.hms(it.get("duration"))
    emit("log", text="搜索「%s」(%s) → %d 条" % (q, method, len(items)))
    return items


def enqueue(items):
    added = []
    for it in items:
        tid = "t%d" % (len(TASKS) + 1)
        task = {"tid": tid, "id": it["id"], "title": it.get("title") or it["id"],
                "artist": it.get("artist") or "", "duration": it.get("duration"),
                "url": "https://www.youtube.com/watch?v=" + it["id"],
                "status": "等待中", "pct": 0, "speed": "", "error": None}
        TASKS.append(task)
        added.append(task_pub(task))
        emit("task", **task_pub(task))
    emit("log", text="入队 %d 首" % len(added))
    return added


def import_wishlist():
    items = hkdl.library_wishlist()
    have = hkdl.local_video_ids()
    todo = [i for i in items if i["id"] not in have]
    if not items:
        emit("log", text="HomeKaraoke 里还没有收藏或排歌记录")
        return []
    if not todo:
        emit("log", text="收藏/排歌记录里的 %d 首都已下载过" % len(items))
        return []
    res = enqueue(todo)
    emit("log", text="导入收藏/排歌 %d 首（跳过本地已有 %d 首）" % (len(res), len(items) - len(todo)))
    return res


SAFARI_COOKIES = os.path.expanduser(
    "~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies")


def cookies_readable(browser):
    """Safari 的 cookie 库受 TCC 保护，没给「完全磁盘访问权限」就抛 Operation not permitted。"""
    if (browser or "").lower() != "safari":
        return True
    try:
        with open(SAFARI_COOKIES, "rb") as fh:
            fh.read(4)
        return True
    except OSError:
        return False


def effective_cookies():
    cookies = STATE["cookies"]
    if not cookies_readable(cookies):
        emit("log", text="Safari 的 Cookie 库读不了（需要「完全磁盘访问权限」），本次自动改用 Chrome")
        return "Chrome"
    return cookies


def start_downloads():
    if STATE["running"]:
        return False
    pending = [t for t in TASKS if t["status"] in ("等待中", None)]
    if not pending:
        emit("log", text="队列为空：先搜歌加入队列")
        return False
    STATE["running"] = True
    STATE["stop"] = False
    out_dir, fmt, cookies = STATE["dir"], STATE["format"], effective_cookies()
    os.makedirs(out_dir, exist_ok=True)
    emit("log", text="开始下载 %d 首 → %s（%s）" % (len(pending), out_dir, fmt))

    def work():
        for task in pending:
            if STATE["stop"]:
                task["status"] = "已取消"
                emit("task", **task_pub(task))
                continue
            task["status"] = "下载中"
            emit("task", **task_pub(task))
            last = [0.0]

            def on_prog(pct, speed, eta, task=task):
                now = time.time()
                if now - last[0] < 0.4:
                    return
                last[0] = now
                task["pct"] = round(pct, 1)
                task["speed"] = (speed or "").strip() + ((" / " + eta.strip())
                                                          if eta and eta not in ("NA", "Unknown") else "")
                emit("task", **task_pub(task))

            try:
                try:
                    path, new_folder = hkdl.download_one(task, out_dir, fmt, cookies, on_prog)
                except Exception as first_err:
                    msg = str(first_err)
                    if cookies.lower() != "chrome" and (
                        "Operation not permitted" in msg or "Cookies.binarycookies" in msg
                    ):
                        emit("log", text="读取 %s Cookie 失败，自动改用 Chrome 重试" % cookies)
                        path, new_folder = hkdl.download_one(
                            task, out_dir, fmt, "Chrome", on_prog)
                task["status"] = "完成·已入库"
                task["pct"] = 100
                size = os.path.getsize(path) / 1048576.0
                task["speed"] = "%.0f MB" % size
                emit("task", **task_pub(task))
                emit("saved", path=path, new_folder=new_folder,
                     text="已入库 %s（%s）" % (os.path.basename(path),
                                              "新增曲库文件夹，请重启 HomeKaraoke"
                                              if new_folder else "HomeKaraoke Local 里可见"))
            except Exception as exc:
                msg = str(exc)
                if "Sign in to confirm" in msg:
                    msg = ("YouTube 机器人验证（Sign in to confirm you're not a bot）："
                           "把底部 Cookie 选成 Chrome 后重试。原始信息：" + msg)
                task["status"] = "失败"
                task["error"] = msg[:300]
                emit("task", **task_pub(task))
                emit("failed", error=str(exc)[:300], text="失败：%s" % str(exc)[:200])
        STATE["running"] = False
        emit("log", text="队列结束")
        emit("done", tasks=[task_pub(t) for t in TASKS])

    threading.Thread(target=work, daemon=True).start()
    return True


def self_check():
    def work():
        yt, ff, fp = hkdl.find_bin("yt-dlp"), hkdl.find_bin("ffmpeg"), hkdl.find_bin("ffprobe")
        emit("log", text="yt-dlp=%s  ffmpeg=%s  ffprobe=%s" % (yt, ff, fp))
        try:
            con = hkdl.db_connect()
            n = con.execute("SELECT COUNT(*) FROM videos WHERE source_type='local'").fetchone()[0]
            folders = [r[0] for r in con.execute("SELECT path FROM library_folders")]
            con.close()
            emit("log", text="数据库可写 · 本地曲目 %d 首 · 曲库文件夹 %s" % (n, folders))
        except Exception as exc:
            emit("log", text="数据库异常：%s" % exc)
        d = STATE["dir"]
        emit("log", text="目标目录 %s %s" % (d, "可写" if os.access(d, os.W_OK) else "不可写"))
        try:
            items = hkdl.search("karaoke", "yt-dlp", 3)
            emit("log", text="YouTube 连通正常（返回 %d 条）" % len(items))
        except Exception as exc:
            emit("log", text="YouTube 搜索失败：%s" % exc)
    threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------ HTTP
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HomeKaraoke 下载器</title>
<link rel="icon" href="data:image/png;base64,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">
<style>
:root{--bg:#0b1020;--card:#151c2e;--card2:#1b2437;--txt:#e8ecf5;--mut:#8b97ad;--acc:#7c6cf6;--ok:#34d399;--bad:#f87171}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#0b1020,#0d1226);color:var(--txt);
 font:14px/1.5 "PingFang SC","Helvetica Neue",Arial,sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:14px 18px;background:#0e1526;border-bottom:1px solid #1e2942}
header h1{font-size:17px;margin:0;font-weight:700;letter-spacing:.5px}
header .sub{color:var(--mut);font-size:12px}
.pill{margin-left:auto;display:flex;gap:8px;align-items:center;color:var(--mut);font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok)}
.bar{display:flex;gap:10px;padding:12px 18px;align-items:center}
input[type=text]{flex:1;background:var(--card);border:1px solid #26314b;color:var(--txt);
 padding:10px 12px;border-radius:10px;font-size:14px;outline:none}
input[type=text]:focus{border-color:var(--acc)}
select{background:var(--card);border:1px solid #26314b;color:var(--txt);padding:9px 10px;border-radius:9px;outline:none}
button{background:var(--card2);border:1px solid #2b3752;color:var(--txt);padding:9px 14px;
 border-radius:10px;cursor:pointer;font-size:13px;transition:.15s}
button:hover{background:#26324c;border-color:#3a4a6b}
button.primary{background:linear-gradient(135deg,#7c6cf6,#a855f7);border:0;font-weight:600}
button.primary:hover{filter:brightness(1.08)}
button:disabled{opacity:.45;cursor:default}
main{flex:1;overflow:auto;padding:0 18px 10px}
section{margin-bottom:16px}
.sec-head{display:flex;align-items:center;gap:10px;color:var(--mut);font-size:12px;
 text-transform:uppercase;letter-spacing:1px;margin:8px 0}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:var(--mut);font-weight:500;font-size:12px;padding:6px 8px;border-bottom:1px solid #1e2942}
td{padding:6px 8px;border-bottom:1px solid #16203a;vertical-align:middle}
tr:hover td{background:#131b2e}
.thumb{width:64px;height:36px;border-radius:6px;object-fit:cover;background:#0f1626}
.title{font-weight:500}
.clip{overflow:hidden;white-space:nowrap;display:block}
.clip>span{display:inline-block;white-space:nowrap;transition:transform .25s ease-out}
.mut{color:var(--mut);font-size:12px}
.task{display:flex;align-items:center;gap:12px;padding:10px 12px;background:var(--card);
 border:1px solid #1d2740;border-radius:12px;margin-bottom:8px}
.task .name{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.track{width:200px;height:7px;background:#0d1424;border-radius:99px;overflow:hidden}
.track i{display:block;height:100%;width:0;background:linear-gradient(90deg,#7c6cf6,#34d399);transition:width .3s}
.st{width:92px;font-size:12px;color:var(--mut)}
.st.ok{color:var(--ok)} .st.bad{color:var(--bad)}
footer{padding:10px 18px 14px;border-top:1px solid #1e2942;background:#0e1526}
footer .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
footer .row + .row{margin-top:8px}
#log{height:96px;overflow:auto;background:#080d18;border:1px solid #16203a;border-radius:10px;
 padding:8px 10px;font:11.5px/1.55 Menlo,monospace;color:#8ea0bd;white-space:pre-wrap}
.empty{color:var(--mut);padding:16px;text-align:center;border:1px dashed #23304a;border-radius:12px}
.tag{font-size:11px;padding:2px 7px;border-radius:99px;background:#1e2942;color:var(--mut)}
</style></head><body>
<header>
  <h1>HomeKaraoke 下载器</h1>
  <span class="sub">搜索 → 下载 → 自动写进 HomeKaraoke 的 Local 曲库</span>
  <div class="pill"><span class="dot" id="dot"></span><span id="tools">连接中…</span></div>
</header>

<div class="bar">
  <input type="text" id="q" placeholder="输入歌名，例如：後來 劉若英 伴奏 / 陳雪凝 膽小鬼">
  <select id="method"><option>自动</option><option>API</option><option>yt-dlp</option></select>
  <button class="primary" id="btn-search">搜索</button>
</div>

<main>
  <section>
    <div class="sec-head"><span id="res-count">搜索结果</span></div>
    <div id="results"><div class="empty">输入歌名后回车或点「搜索」</div></div>
  </section>

  <section>
    <div class="sec-head"><span>下载队列</span><span class="tag" id="q-count">0</span></div>
    <div id="queue"><div class="empty">队列是空的：先在搜索结果里勾选，再点「加入下载队列」</div></div>
  </section>
</main>

<footer>
  <div class="row">
    <button id="btn-add" class="primary">↓ 加入下载队列</button>
    <button id="btn-import">导入收藏 / 排歌记录</button>
    <button id="btn-start">开始下载</button>
    <button id="btn-stop" disabled>停止</button>
    <button id="btn-check">自检</button>
    <button id="btn-restart">重启 HomeKaraoke</button>
    <button id="btn-folder">打开文件夹</button>
  </div>
  <div class="row">
    <span class="mut">保存到</span>
    <input type="text" id="dir" style="flex:1;padding:7px 10px">
    <select id="format">
      <option>视频 MP4 (最高1080p)</option><option>视频 MP4 (720p)</option><option>仅音频 M4A</option>
    </select>
    <span class="mut">Cookie</span>
    <select id="cookies"><option>无</option><option>Safari</option><option>Chrome</option><option>Edge</option><option>Firefox</option></select>
  </div>
  <div class="row"><div id="log" style="width:100%"></div></div>
</footer>

<script>
const $ = s => document.querySelector(s);
let RESULTS = [], TASKS = {}, seq = 0;

function log(text){
  const el = $("#log");
  el.textContent += text + "\n";
  if (el.textContent.length > 20000) el.textContent = el.textContent.slice(-12000);
  el.scrollTop = el.scrollHeight;
}

function renderResults(){
  const box = $("#results");
  if (!RESULTS.length){ box.innerHTML = '<div class="empty">没有结果</div>'; return; }
  box.innerHTML = '<table><thead><tr><th style="width:32px"><input type="checkbox" id="all"></th>'+
    '<th style="width:80px">封面</th><th>歌曲标题</th><th style="width:230px">频道 / 歌手</th>'+
    '<th style="width:70px">时长</th><th style="width:70px">来源</th></tr></thead><tbody>'+
    RESULTS.map((r,i)=>`<tr><td><input type="checkbox" class="pick" data-i="${i}"></td>
      <td><img class="thumb" loading="lazy" src="${r.thumb}" alt=""></td>
      <td class="title"><span class="clip" title="${esc(r.title)}"><span>${esc(r.title)}</span></span></td><td class="mut">${esc(r.artist||"")}</td>
      <td class="mut">${r.dur||""}</td><td class="mut">${esc(r.source||"")}</td></tr>`).join("")+
    '</tbody></table>';
  $("#res-count").textContent = "搜索结果 · " + RESULTS.length + " 条";
  const all = $("#all");
  if (all) all.onchange = () => document.querySelectorAll(".pick").forEach(c=>c.checked=all.checked);
}
function esc(s){ return (s||"").replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function renderQueue(){
  const ids = Object.keys(TASKS);
  $("#q-count").textContent = ids.length;
  const box = $("#queue");
  if (!ids.length){ box.innerHTML = '<div class="empty">队列是空的：先在搜索结果里勾选，再点「加入下载队列」</div>'; return; }
  box.innerHTML = ids.map(id => {
    const t = TASKS[id];
    const cls = t.status.includes("完成") ? "ok" : (t.status.includes("失败")?"bad":"");
    return `<div class="task"><span class="name clip" title="${esc(t.title)}"><span>${esc(t.title)}</span></span>
      <span class="mut" style="width:0"></span>
      <span class="track"><i style="width:${t.pct||0}%"></i></span>
      <span class="st ${cls}">${esc(t.status)}</span>
      <span class="mut" style="width:150px;text-align:right">${esc(t.speed||"")}</span></div>`;
  }).join("");
}

async function api(path, body){
  const opt = body ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return r.json();
}

async function refreshState(){
  const s = await api("/api/state");
  $("#dir").value = s.dir; $("#format").value = s.format;
  // 以服务端状态为准，避免旧页面把 Cookie 选择覆盖回「无」
  if (s.cookies !== undefined && s.cookies !== null) $("#cookies").value = s.cookies;
  $("#tools").textContent = "yt-dlp " + (s.ytdlp?"就绪":"缺失") + " · ffmpeg " + (s.ffmpeg?"就绪":"缺失")
    + " · 本地曲目 " + s.local_count;
  TASKS = {}; s.tasks.forEach(t => TASKS[t.tid] = t); renderQueue();
  $("#btn-start").disabled = s.running; $("#btn-stop").disabled = !s.running;
}

async function search(){
  const q = $("#q").value.trim();
  if (!q) return;
  $("#btn-search").disabled = true; $("#res-count").textContent = "搜索中…";
  try {
    const r = await api("/api/search", {q, method: $("#method").value});
    if (r.error){ log("搜索失败：" + r.error); $("#res-count").textContent = "搜索失败"; }
    else { RESULTS = r.items; renderResults(); }
  } finally { $("#btn-search").disabled = false; }
}

$("#btn-search").onclick = search;
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") search(); });
$("#btn-add").onclick = async () => {
  const picks = [...document.querySelectorAll(".pick:checked")].map(c => RESULTS[+c.dataset.i]);
  if (!picks.length){ log("先勾选要下载的歌"); return; }
  const r = await api("/api/enqueue", {items: picks});
  r.tasks.forEach(t => TASKS[t.tid] = t); renderQueue();
};
$("#btn-import").onclick = async () => {
  const r = await api("/api/import", {});
  (r.tasks||[]).forEach(t => TASKS[t.tid] = t); renderQueue();
};
$("#btn-start").onclick = async () => {
  await api("/api/config", {dir: $("#dir").value, format: $("#format").value, cookies: $("#cookies").value});
  await api("/api/start", {});
  refreshState();
};
$("#cookies").addEventListener("change", async () => {
  await api("/api/config", {dir: $("#dir").value, format: $("#format").value, cookies: $("#cookies").value});
  log("Cookie 来源已设为 " + $("#cookies").value);
});
$("#btn-stop").onclick = () => api("/api/stop", {});
$("#btn-check").onclick = () => api("/api/selfcheck", {});
$("#btn-restart").onclick = () => api("/api/restart-hk", {});
$("#btn-folder").onclick = () => api("/api/open-folder", {});

function stream(){
  const es = new EventSource("/api/events?from=" + seq);
  es.onmessage = ev => {
    const e = JSON.parse(ev.data); seq = e.seq;
    if (e.kind === "log" || e.kind === "saved" || e.kind === "failed") log(e.text || e.error || "");
    if (e.kind === "task"){ TASKS[e.tid] = {tid:e.tid,id:e.id,title:e.title,status:e.status,pct:e.pct,speed:e.speed}; renderQueue(); }
    if (e.kind === "done"){ refreshState(); }
  };
  es.onerror = () => { es.close(); setTimeout(stream, 1500); };
}

renderResults(); refreshState(); stream();
setInterval(() => fetch("/api/ping").catch(()=>{}), 20000);
log("界面已就绪");

// 悬停把截断的歌名滑动显示全称
function clipInner(el){ return el && el.firstElementChild; }
document.addEventListener("mouseover", (e) => {
  const clip = e.target.closest && e.target.closest(".clip");
  if (!clip) return;
  if (e.relatedTarget && clip.contains(e.relatedTarget)) return;
  const inner = clipInner(clip);
  if (!inner) return;
  const over = inner.scrollWidth - clip.clientWidth;
  if (over <= 4) return;
  inner.style.transition = "transform " + Math.min(14, Math.max(1.6, over / 45)) + "s linear";
  inner.style.transform = "translateX(-" + over + "px)";
});
document.addEventListener("mouseout", (e) => {
  const clip = e.target.closest && e.target.closest(".clip");
  if (!clip) return;
  if (e.relatedTarget && clip.contains(e.relatedTarget)) return;
  const inner = clipInner(clip);
  if (!inner) return;
  inner.style.transition = "transform .25s ease-out";
  inner.style.transform = "translateX(0)";
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if u.path == "/api/ping":
            STATE["last_ping"] = time.time()
            return self._send(200, {"ok": True})
        if u.path == "/api/state":
            try:
                con = hkdl.db_connect()
                n = con.execute(
                    "SELECT COUNT(*) FROM videos WHERE source_type='local'").fetchone()[0]
                con.close()
            except Exception:
                n = -1
            return self._send(200, {
                "dir": STATE["dir"], "format": STATE["format"], "cookies": STATE["cookies"],
                "running": STATE["running"],
                "ytdlp": bool(hkdl.find_bin("yt-dlp")), "ffmpeg": bool(hkdl.find_bin("ffmpeg")),
                "local_count": n,
                "tasks": [task_pub(t) for t in TASKS],
            })
        if u.path == "/api/events":
            qs = urllib.parse.parse_qs(u.query)
            start = int(qs.get("from", ["0"])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            cursor = start
            STREAMS["active"] += 1
            try:
                while True:
                    with LOCK:
                        pending = [e for e in BUS["items"] if e["seq"] > cursor]
                    for ev in pending:
                        cursor = ev["seq"]
                        self.wfile.write(("data: %s\n\n" % json.dumps(ev, ensure_ascii=False)).encode())
                    if not pending:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    STATE["last_ping"] = time.time()
                    time.sleep(0.4)
            except Exception:
                return
            finally:
                STREAMS["active"] = max(0, STREAMS["active"] - 1)
                STREAMS["last_closed"] = time.time()
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        data = self._body()
        try:
            if u.path == "/api/search":
                items = do_search((data.get("q") or "").strip(), data.get("method") or "自动")
                return self._send(200, {"items": items})
            if u.path == "/api/enqueue":
                return self._send(200, {"tasks": enqueue(data.get("items") or [])})
            if u.path == "/api/import":
                return self._send(200, {"tasks": import_wishlist()})
            if u.path == "/api/config":
                STATE["dir"] = data.get("dir") or STATE["dir"]
                STATE["format"] = data.get("format") or STATE["format"]
                STATE["cookies"] = data.get("cookies") or STATE["cookies"]
                hkdl.save_cfg({"dir": STATE["dir"], "format": STATE["format"],
                               "cookies": STATE["cookies"]})
                return self._send(200, {"ok": True})
            if u.path == "/api/start":
                return self._send(200, {"started": start_downloads()})
            if u.path == "/api/stop":
                STATE["stop"] = True
                emit("log", text="正在停止…")
                return self._send(200, {"ok": True})
            if u.path == "/api/selfcheck":
                self_check()
                return self._send(200, {"ok": True})
            if u.path == "/api/restart-hk":
                subprocess.run(["osascript", "-e",
                                'tell application "HomeKaraoke" to quit'], capture_output=True)
                time.sleep(2)
                subprocess.run(["open", "-a", "HomeKaraoke"])
                emit("log", text="已重启 HomeKaraoke → 进 Local 点「扫描」")
                return self._send(200, {"ok": True})
            if u.path == "/api/open-folder":
                os.makedirs(STATE["dir"], exist_ok=True)
                subprocess.run(["open", STATE["dir"]])
                return self._send(200, {"ok": True})
        except Exception as exc:
            emit("failed", error=str(exc)[:300], text="操作失败：%s" % str(exc)[:200])
            return self._send(200, {"error": str(exc)[:300]})
        return self._send(404, {"error": "not found"})


def watchdog():
    """界面关掉后自动退出，别留后台进程。

    存活判断优先看 SSE 长连接：Chrome 对后台窗口会节流定时器，心跳会停，
    但已经建立的 EventSource 连接仍然在。只有在没有任何界面连接时才退出。
    """
    while True:
        time.sleep(15)
        if STATE["running"]:
            continue
        idle_streams = time.time() - STREAMS["last_closed"]
        if STREAMS["active"] == 0 and idle_streams > 300 and \
                time.time() - STATE["last_ping"] > 900:
            file_log("界面已关闭 %ds，退出" % int(idle_streams))
            os._exit(0)


def main(argv):
    if "--open-ui" in argv:
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    emit("log", text="服务启动 http://127.0.0.1:%d" % PORT)
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
