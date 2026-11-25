import os
import re
import time
import base64
import asyncio
from urllib.parse import quote

import httpx
import yt_dlp
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response

app = FastAPI(title="StreamVault Turbo")

STREAM_CACHE = {}
CACHE_TTL_SEC = 1500
UA_HEADER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

COOKIE_FILE_PATH = "/tmp/yt_cookies.txt"
if os.environ.get("YT_COOKIES_B64"):
    try:
        with open(COOKIE_FILE_PATH, "wb") as f:
            f.write(base64.b64decode(os.environ["YT_COOKIES_B64"].strip()))
    except Exception:
        pass


def b64u_encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> str:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii")).decode("utf-8", errors="ignore")


def fmt_duration(sec) -> str:
    try:
        s = int(sec or 0)
        if s <= 0:
            return "HD"
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return "HD"


def _yt_search_sync(query: str, limit: int = 24, category: str = "", no_shorts: bool = False):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
        "socket_timeout": 8,
    }
    if os.path.isfile(COOKIE_FILE_PATH):
        opts["cookiefile"] = COOKIE_FILE_PATH

    out = []
    seen = set()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            entries = (info or {}).get("entries") or []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                vid = str(e.get("id") or "").strip()
                if len(vid) != 11 or vid in seen:
                    continue
                dur_sec = int(e.get("duration") or 0)
                title = str(e.get("title") or "YouTube Video").strip()
                if no_shorts and ((0 < dur_sec < 55) or "#shorts" in title.lower()):
                    continue
                seen.add(vid)
                hd_thumb = f"https://i.ytimg.com/vi/{vid}/hq720.jpg"
                hq_thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                views = e.get("view_count")
                views_str = ""
                if isinstance(views, int) and views > 0:
                    if views >= 1_000_000:
                        views_str = f"{views / 1_000_000:.1f}M views"
                    elif views >= 1_000:
                        views_str = f"{views / 1_000:.0f}K views"
                    else:
                        views_str = f"{views} views"
                out.append({
                    "id": vid,
                    "title": title,
                    "author": str(e.get("channel") or e.get("uploader") or "YouTube"),
                    "duration": fmt_duration(dur_sec),
                    "views": views_str,
                    "category": category,
                    "thumbHd": f"/img?u={b64u_encode(hd_thumb)}",
                    "thumbHq": f"/img?u={b64u_encode(hq_thumb)}",
                })
    except Exception:
        pass
    return out


def _yt_resolve_sync(video_id: str, quality: str = "360"):
    cache_key = (video_id, quality)
    cached = STREAM_CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"] < CACHE_TTL_SEC):
        return cached

    fmt = (
        "18/best[height<=480][ext=mp4][vcodec!=none][acodec!=none]/"
        "best[ext=mp4][vcodec!=none][acodec!=none]/best"
        if quality == "360"
        else "22/best[height<=720][ext=mp4][vcodec!=none][acodec!=none]/"
             "18/best[ext=mp4][vcodec!=none][acodec!=none]/best"
    )

    client_sets = [
        ["android_vr", "ios"],
        ["tv_embedded", "android"],
        ["web", "mweb"],
    ]
    last_err = "Unable to extract stream"

    for clients in client_sets:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": fmt,
            "socket_timeout": 8,
            "extractor_args": {"youtube": {"player_client": clients}},
        }
        if os.path.isfile(COOKIE_FILE_PATH):
            opts["cookiefile"] = COOKIE_FILE_PATH
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                if not isinstance(info, dict):
                    continue
                stream_url = info.get("url")
                if not stream_url and info.get("formats"):
                    for f in reversed(info["formats"]):
                        if f.get("url") and f.get("vcodec") != "none" and f.get("acodec") != "none":
                            stream_url = f["url"]
                            break
                if stream_url:
                    res = {
                        "url": stream_url,
                        "headers": info.get("http_headers") or {"User-Agent": UA_HEADER},
                        "title": str(info.get("title") or f"Video {video_id}"),
                        "height": info.get("height") or (360 if quality == "360" else 720),
                        "ts": time.time(),
                    }
                    STREAM_CACHE[cache_key] = res
                    return res
        except Exception as exc:
            last_err = str(exc)

    raise RuntimeError(last_err)


async def proxy_stream_response(target_url: str, request: Request, extra_headers=None, default_mime="video/mp4"):
    req_headers = {"User-Agent": UA_HEADER, "Accept": "*/*", "Accept-Encoding": "identity"}
    if extra_headers:
        for k, v in extra_headers.items():
            if k.lower() not in ("host", "content-length", "accept-encoding"):
                req_headers[k] = v
    range_hdr = request.headers.get("range")
    if range_hdr:
        req_headers["Range"] = range_hdr

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(12.0, read=60.0))
    req = client.build_request("GET", target_url, headers=req_headers)
    try:
        upstream = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        return Response("Upstream connection failed", status_code=502)

    resp_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    for h in ("content-type", "content-length", "content-range"):
        if h in upstream.headers:
            resp_headers[h.title()] = upstream.headers[h]
    if "Content-Type" not in resp_headers:
        resp_headers["Content-Type"] = default_mime

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(body_iter(), status_code=upstream.status_code, headers=resp_headers)


@app.get("/img")
async def img_proxy(u: str = Query(...)):
    url = b64u_decode(u)
    if not re.match(r"^https?://", url, re.I):
        return Response(status_code=400)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=6.0) as client:
            r = await client.get(url, headers={"User-Agent": UA_HEADER})
            return Response(
                content=r.content,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except Exception:
        return Response(status_code=404)


@app.get("/api/search")
async def api_search(
    q: str = Query(""),
    limit: int = Query(20, ge=4, le=40),
    cat: str = Query(""),
    noshorts: int = Query(0),
):
    q = q.strip()
    if not q:
        return JSONResponse({"status": "ok", "items": []})
    items = await asyncio.to_thread(_yt_search_sync, q, limit, cat, noshorts == 1)
    return JSONResponse({"status": "ok", "items": items})


@app.get("/api/resolve")
async def api_resolve(id: str = Query(...), q: str = Query("360")):
    vid = re.sub(r"[^a-zA-Z0-9_-]", "", id)[:11]
    quality = "720" if q == "720" else "360"
    try:
        data = await asyncio.to_thread(_yt_resolve_sync, vid, quality)
        return JSONResponse({
            "status": "ok",
            "videoId": vid,
            "title": data["title"],
            "height": data["height"],
            "streamUrl": f"/stream/{vid}?q={quality}",
            "thumb": f"/img?u={b64u_encode(f'https://i.ytimg.com/vi/{vid}/hq720.jpg')}",
        })
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.get("/stream/{video_id}")
async def stream_video(video_id: str, request: Request, q: str = Query("360")):
    vid = re.sub(r"[^a-zA-Z0-9_-]", "", video_id)[:11]
    quality = "720" if q == "720" else "360"
    try:
        data = await asyncio.to_thread(_yt_resolve_sync, vid, quality)
    except Exception as exc:
        return Response(f"Stream error: {exc}", status_code=502)
    return await proxy_stream_response(data["url"], request, data.get("headers"), "video/mp4")


@app.get("/drive_stream/{file_id}")
async def drive_stream(file_id: str, request: Request):
    fid = re.sub(r"[^a-zA-Z0-9_-]", "", file_id)
    target = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    return await proxy_stream_response(target, request, None, "video/mp4")


@app.get("/api/drive_folder")
async def api_drive_folder(id: str = Query(...)):
    fid = re.sub(r"[^a-zA-Z0-9_-]", "", id)
    files = []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
            r = await client.get(f"https://drive.google.com/embeddedfolderview?id={quote(fid)}#list", headers={"User-Agent": UA_HEADER})
            html = r.text
            for m in re.finditer(r'<div class="flip-entry" id="entry-([a-zA-Z0-9_-]+)".*?<div class="flip-entry-title">(.*?)</div>', html, re.S):
                entry_id = m.group(1).strip()
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip() or f"Drive_Video_{entry_id[:6]}"
                is_folder = "drive.google.com/drive/folders/" in m.group(0)
                files.append({"id": entry_id, "name": title, "isFolder": is_folder})
    except Exception:
        pass
    return JSONResponse({"status": "ok", "files": files})


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5">
<title>StreamVault Turbo — 100% Server-Proxy Video Player</title>
<style>
:root {
  --bg:#060814; --surface:#0e1326; --card:#121933; --cyan:#00f5d4; --blue:#38bdf8;
  --pink:#ff2e7e; --purple:#a855f7; --gold:#fbbf24; --green:#22c55e; --text:#f8fafc; --muted:#94a3b8;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
body{
  background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;
  background-image:
    radial-gradient(circle at 10% 4%,rgba(255,46,126,0.16),transparent 30%),
    radial-gradient(circle at 90% 8%,rgba(0,245,212,0.15),transparent 32%);
  padding-bottom:48px;
}
.shell{max-width:1460px;margin:0 auto;padding:12px 15px;}
.topbar{
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;
  padding:12px 16px;background:linear-gradient(135deg,#10162f,#1b1238);
  border:1px solid #2b3f73;border-radius:16px;margin-bottom:12px;
}
.brand{display:flex;align-items:center;gap:11px;cursor:pointer;}
.brand-ic{
  width:42px;height:42px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#ff6b00,#ff2e7e,#a855f7,#00f5d4);color:#fff;font-size:20px;font-weight:900;
}
.brand h1{
  font-size:19px;font-weight:900;
  background:linear-gradient(90deg,#00f5d4,#38bdf8,#fbbf24,#ff2e7e);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.brand small{display:block;font-size:11px;color:#cbd5e1;}
.badge{
  padding:7px 12px;border-radius:999px;font-size:11.5px;font-weight:900;
  background:linear-gradient(135deg,#10b981,#00f5d4);color:#031915;
}
.search-box{background:var(--surface);border:1px solid #223055;border-radius:16px;padding:12px;margin-bottom:13px;}
.row{display:flex;gap:8px;flex-wrap:wrap;}
.inp{
  flex:1;min-width:210px;background:#070b18;color:#fff;border:1.5px solid #2b3f73;
  border-radius:11px;padding:11px 14px;font-size:14px;outline:none;
}
.inp:focus{border-color:var(--cyan);}
button{
  font:inherit;cursor:pointer;border-radius:10px;padding:8px 13px;font-size:12.5px;font-weight:800;
  border:1px solid #2d4278;background:#172347;color:#fff;display:inline-flex;align-items:center;gap:6px;
}
button:hover{border-color:var(--cyan);}
button.primary{background:linear-gradient(135deg,#00f5d4,#38bdf8);color:#04101d;border:none;}
button.gold{background:linear-gradient(135deg,#fbbf24,#fb923c);color:#1c0f00;border:none;}
button.pink{background:linear-gradient(135deg,#ff2e7e,#a855f7);color:#fff;border:none;}
button.danger{background:#3b1224;border-color:#ff2e7e;color:#fecdd3;}
.cats{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px;}
.cat{padding:7px 13px;border-radius:999px;font-size:12px;background:#111936;border:1px solid #2c3c68;}
.cat.active{background:linear-gradient(135deg,#00f5d4,#38bdf8,#a855f7);color:#04101d;border:none;}

.player-card{
  background:var(--surface);border:1.5px solid #28467a;border-radius:18px;
  overflow:hidden;margin-bottom:14px;box-shadow:0 12px 32px rgba(0,0,0,0.5);
}
.p-head{
  display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;
  padding:10px 14px;background:#111936;border-bottom:1px solid #1f2e57;
}
.p-title{font-size:14px;font-weight:800;color:#fff;flex:1;min-width:170px;}
.p-pill{font-size:10.5px;font-weight:900;padding:4px 10px;border-radius:999px;background:linear-gradient(135deg,#00f5d4,#38bdf8);color:#04101d;}
.stage{
  position:relative;width:100%;aspect-ratio:16/9;max-height:72vh;background:#000;
  display:flex;align-items:center;justify-content:center;overflow:hidden;
}
#vid{width:100%;height:100%;object-fit:contain;background:#000;display:none;}
#vid.active{display:block;}
.placeholder{text-align:center;padding:24px;color:var(--muted);max-width:520px;}
.placeholder strong{display:block;color:#fff;font-size:16px;margin-bottom:6px;}
.loader{
  position:absolute;inset:0;background:rgba(5,8,18,0.88);display:none;
  flex-direction:column;align-items:center;justify-content:center;gap:10px;z-index:5;
}
.loader.show{display:flex;}
.spin{width:38px;height:38px;border-radius:50%;border:3px solid #1e335c;border-top-color:var(--cyan);animation:sp 0.7s linear infinite;}
@keyframes sp{to{transform:rotate(360deg);}}
.ctrls{
  display:flex;align-items:center;justify-content:space-between;gap:6px;flex-wrap:wrap;
  padding:9px 13px;background:#0c1328;border-top:1px solid #1d2a4e;
}
.grp{display:flex;gap:6px;flex-wrap:wrap;align-items:center;}
.ctrls button{padding:7px 10px;font-size:11.5px;}
.save-bar{
  display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;
  padding:10px 14px;background:linear-gradient(90deg,#271f0e,#241028,#0b2429);border-top:1px solid #594418;
}
.tabs{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;}
.tab{flex:1;min-width:120px;padding:11px;background:var(--surface);border-radius:12px;}
.tab.active{background:linear-gradient(135deg,#00f5d4,#38bdf8);color:#04101d;border:none;}
.pane{display:none;}.pane.active{display:block;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px;}
.card{
  background:var(--card);border:1px solid #23335e;border-radius:14px;overflow:hidden;
  display:flex;flex-direction:column;cursor:pointer;transition:transform 0.14s,border-color 0.14s;
}
.card:hover{border-color:var(--cyan);transform:translateY(-3px);}
.twrap{position:relative;width:100%;aspect-ratio:16/9;background:#040712;overflow:hidden;}
.twrap img{width:100%;height:100%;object-fit:cover;display:block;}
.tag{position:absolute;top:7px;left:7px;font-size:10px;font-weight:900;padding:3px 8px;border-radius:999px;background:linear-gradient(135deg,#ff2e7e,#a855f7);color:#fff;}
.dur{position:absolute;bottom:7px;right:7px;background:rgba(4,7,18,0.92);color:#fff;font-size:11px;font-weight:800;padding:2px 7px;border-radius:6px;}
.cbody{padding:11px;display:flex;flex-direction:column;gap:6px;flex:1;}
.ctitle{font-size:13.5px;font-weight:800;color:#fff;line-height:1.36;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.cmeta{font-size:11.5px;color:var(--muted);}
.cacts{display:flex;gap:7px;margin-top:auto;padding-top:6px;}
.cacts button{flex:1;padding:7px;font-size:12px;}
.list{display:flex;flex-direction:column;gap:9px;}
.litem{
  display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;
  padding:12px 14px;background:var(--card);border:1px solid #23335e;border-radius:13px;
}
.empty{text-align:center;padding:28px;color:#cbd5e1;background:var(--surface);border:1.5px dashed #2b3f73;border-radius:15px;}
.more-wrap{display:flex;justify-content:center;margin-top:16px;}
.toast-box{position:fixed;bottom:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:7px;}
.toast{background:#0f172a;border:1.5px solid var(--cyan);color:#fff;padding:10px 14px;border-radius:11px;font-size:12.5px;font-weight:700;}
@media(max-width:640px){.grid{grid-template-columns:1fr;}}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="brand" onclick="selectCat('all')">
      <div class="brand-ic">▶</div>
      <div>
        <h1>StreamVault Turbo</h1>
        <small>100% Server-Proxy YouTube + Google Drive • Zero Direct YouTube Traffic</small>
      </div>
    </div>
    <span class="badge">🛡️ COLLEGE PROXY ENGINE ACTIVE</span>
  </header>

  <section class="search-box">
    <div class="row">
      <input id="qInput" class="inp" placeholder="Search YouTube videos or paste YouTube / Google Drive link..." onkeydown="if(event.key==='Enter')handleInput()">
      <button class="primary" onclick="handleInput()">🔍 Search / Play</button>
      <button class="gold" onclick="saveInputLink()">⭐ Save Link</button>
    </div>
    <div class="cats">
      <button class="cat active" id="c-all" onclick="selectCat('all')">🔥 All Mix</button>
      <button class="cat" id="c-db" onclick="selectCat('db')">🐉 Dragon Ball</button>
      <button class="cat" id="c-romcom" onclick="selectCat('romcom')">💕 Rom-Com Anime</button>
      <button class="cat" id="c-space" onclick="selectCat('space')">🚀 Space</button>
      <button class="cat" id="c-trading" onclick="selectCat('trading')">📈 Trading</button>
      <button class="cat" id="c-coding" onclick="selectCat('coding')">💻 Coding</button>
    </div>
  </section>

  <section class="player-card" id="pCard">
    <div class="p-head">
      <div class="p-title" id="nowTitle">🎬 Tap any video below to play smoothly through our own proxy player</div>
      <span class="p-pill" id="pBadge">READY</span>
    </div>
    <div class="stage" id="stage">
      <div class="loader" id="loader"><div class="spin"></div><div id="loadTxt" style="color:var(--cyan);font-weight:800;font-size:13px;">Loading Video Stream...</div></div>
      <div class="placeholder" id="ph">
        <strong>⚡ Own HTML5 Video Player (100% Server-Proxied)</strong>
        <p>Every search, thumbnail, and video stream comes strictly from this website server — fast 360p College Mode or 720p HD!</p>
      </div>
      <video id="vid" controls playsinline preload="auto"></video>
    </div>
    <div class="ctrls">
      <div class="grp">
        <button class="primary" onclick="togglePlay()">⏯ Play / Pause</button>
        <button onclick="seek(-10)">⏪ -10s</button>
        <button onclick="seek(10)">⏩ +10s</button>
        <button onclick="cycleSpeed()" id="spdBtn">⏩ 1.0x</button>
        <button onclick="toggleLoop()" id="loopBtn">🔁 Loop: OFF</button>
      </div>
      <div class="grp">
        <button class="pink" id="q360Btn" onclick="setQuality('360')">⚡ 360p Fast (College)</button>
        <button id="q720Btn" onclick="setQuality('720')">🎬 720p HD</button>
        <button class="gold" onclick="goFull()">⛶ Fullscreen</button>
      </div>
    </div>
    <div class="save-bar">
      <div id="saveInfo" style="font-size:12.5px;color:#fef3c7;font-weight:700;">⭐ Save currently playing YouTube or Google Drive link</div>
      <button class="gold" onclick="saveCurrent()">⭐ Save Current Link</button>
    </div>
  </section>

  <div class="tabs">
    <button class="tab active" id="tb-yt" onclick="switchTab('yt')">🎬 Videos (<span id="ytCnt">0</span>)</button>
    <button class="tab" id="tb-drive" onclick="switchTab('drive')">☁️ Google Drive (<span id="drCnt">0</span>)</button>
    <button class="tab" id="tb-saved" onclick="switchTab('saved')">⭐ Saved Vault (<span id="svCnt">0</span>)</button>
  </div>

  <section class="pane active" id="pn-yt">
    <div class="grid" id="grid"></div>
    <div class="more-wrap" id="moreWrap" style="display:none;"><button class="primary" id="moreBtn" onclick="loadNextWave(true)">⚡ Load More Videos</button></div>
  </section>
  <section class="pane" id="pn-drive"><div class="list" id="drList"><div class="empty">Paste a Google Drive File or Folder link above.</div></div></section>
  <section class="pane" id="pn-saved"><div class="list" id="svList"></div></section>
</div>
<div class="toast-box" id="toasts"></div>

<script>
const $ = (id) => document.getElementById(id);
const vid = $('vid');
let quality = '360';
let speeds = [0.75, 1.0, 1.25, 1.5, 2.0], spdIdx = 1;
let curMedia = { type:'', id:'', title:'', url:'' };
let saved = JSON.parse(localStorage.getItem('sv_turbo_saved') || '[]');

const WAVES = {
  db: ['Dragon Ball Super Broly Goku Vegeta full fight HD', 'Dragon Ball Super Ultra Instinct Goku vs Jiren HD', 'Dragon Ball Z Gohan Super Saiyan 2 best fights HD'],
  romcom: ['Best Rom Com Anime funny romantic moments English sub', 'Horimiya Kaguya sama Love is War funniest scenes HD', 'Oregairu Toradora My Dress Up Darling romance comedy moments'],
  space: ['Black holes universe space documentary 4K NASA', 'James Webb Space Telescope discoveries space 4K', 'Interstellar wormhole planets cosmos documentary'],
  trading: ['Smart Money Concepts price action trading full course', 'Candlestick patterns liquidity order block trading strategy', 'Crypto Forex day trading technical analysis strategy'],
  coding: ['Python programming full course real projects tutorial', 'Full stack JavaScript React web development project', 'C++ Data Structures Algorithms DSA coding course']
};
let feed = { mode:'all', q:'', wave:0, loading:false, items:[], seen:new Set() };

function esc(s){return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
function toast(m){const d=document.createElement('div');d.className='toast';d.textContent=m;$('toasts').appendChild(d);setTimeout(()=>d.remove(),3000);}
function setLoad(on,t){$('loader').classList.toggle('show',on);if(t)$('loadTxt').textContent=t;}
function switchTab(t){['yt','drive','saved'].forEach(x=>{$('tb-'+x).classList.toggle('active',x===t);$('pn-'+x).classList.toggle('active',x===t);});}

function setQuality(q){
  quality = q;
  $('q360Btn').className = q==='360' ? 'pink' : '';
  $('q720Btn').className = q==='720' ? 'pink' : '';
  toast(q==='360' ? '⚡ 360p Fast College Mode Active!' : '🎬 720p HD Mode Active!');
  if(curMedia.type==='youtube' && curMedia.id) playYT(curMedia.id, curMedia.title);
}

async function playYT(id, title){
  $('ph').style.display='none';
  setLoad(true, `Preparing ${quality}p Proxy Stream...`);
  curMedia = { type:'youtube', id, title:title||id, url:`https://www.youtube.com/watch?v=${id}` };
  $('nowTitle').textContent = `▶ ${curMedia.title}`;
  $('saveInfo').textContent = `🎬 YouTube: ${curMedia.title}`;
  $('pCard').scrollIntoView({behavior:'smooth', block:'start'});
  try{
    const r = await fetch(`/api/resolve?id=${encodeURIComponent(id)}&q=${quality}`);
    const d = await r.json();
    if(d.status==='ok' && d.streamUrl){
      if(d.title){ curMedia.title = d.title; $('nowTitle').textContent = `▶ ${d.title}`; }
      $('pBadge').textContent = `PROXY ${d.height||quality}P MP4`;
      vid.classList.add('active');
      vid.poster = d.thumb || '';
      vid.src = d.streamUrl;
      vid.load();
      vid.play().catch(()=>{});
    } else {
      setLoad(false);
      toast('❌ Stream error: ' + (d.message || 'Try switching 360p/720p'));
    }
  }catch(e){
    setLoad(false);
    toast('⚠️ Connection error: ' + e.message);
  }
}

function playDrive(id, title){
  $('ph').style.display='none';
  setLoad(true, 'Streaming Google Drive via Server Proxy...');
  curMedia = { type:'drive-file', id, title:title||id, url:`https://drive.google.com/file/d/${id}/view` };
  $('nowTitle').textContent = `☁️ ${curMedia.title}`;
  $('pBadge').textContent = 'DRIVE PROXY MP4';
  $('saveInfo').textContent = `☁️ Drive: ${curMedia.title}`;
  vid.classList.add('active');
  vid.removeAttribute('poster');
  vid.src = `/drive_stream/${encodeURIComponent(id)}`;
  vid.load();
  vid.play().catch(()=>{});
  $('pCard').scrollIntoView({behavior:'smooth', block:'start'});
}

async function openDriveFolder(id, url){
  switchTab('drive');
  const box = $('drList');
  box.innerHTML = `<div class="empty">⏳ Loading Google Drive folder...</div>`;
  curMedia = { type:'drive-folder', id, title:'📁 Drive Folder '+id.slice(0,6), url:url||`https://drive.google.com/drive/folders/${id}` };
  $('saveInfo').textContent = `📁 Drive Folder: ${curMedia.url}`;
  const r = await fetch(`/api/drive_folder?id=${encodeURIComponent(id)}`);
  const d = await r.json();
  const files = d.files || [];
  $('drCnt').textContent = files.length;
  if(!files.length){ box.innerHTML = `<div class="empty">No public files found. Ensure folder is set to "Anyone with the link".</div>`; return; }
  box.innerHTML = '';
  files.forEach(f=>{
    const row = document.createElement('div');
    row.className = 'litem';
    row.innerHTML = `<div style="cursor:pointer;flex:1;"><strong>${f.isFolder?'📁':'🎬'} ${esc(f.name)}</strong></div>
      <div style="display:flex;gap:6px;"><button class="primary">${f.isFolder?'📂 Open':'▶ Play'}</button><button class="gold">⭐ Save</button></div>`;
    const act = () => f.isFolder ? openDriveFolder(f.id, `https://drive.google.com/drive/folders/${f.id}`) : playDrive(f.id, f.name);
    row.firstChild.onclick = act;
    const bs = row.querySelectorAll('button');
    bs[0].onclick = act;
    bs[1].onclick = () => addSaved({ title:(f.isFolder?'📁 ':'🎬 ')+f.name, url:f.isFolder?`https://drive.google.com/drive/folders/${f.id}`:`https://drive.google.com/file/d/${f.id}/view`, type:f.isFolder?'drive-folder':'drive-file', id:f.id });
    box.appendChild(row);
  });
}

vid.addEventListener('canplay', ()=>setLoad(false));
vid.addEventListener('playing', ()=>setLoad(false));
vid.addEventListener('waiting', ()=>setLoad(true,'Buffering...'));
vid.addEventListener('error', ()=>{ setLoad(false); toast('⚠️ Video failed. Try switching 360p/720p.'); });

function togglePlay(){ if(vid.src) vid.paused ? vid.play().catch(()=>{}) : vid.pause(); }
function seek(s){ if(vid.src) vid.currentTime = Math.max(0, Math.min(vid.duration||99999, (vid.currentTime||0)+s)); }
function cycleSpeed(){ spdIdx=(spdIdx+1)%speeds.length; vid.playbackRate=speeds[spdIdx]; $('spdBtn').textContent=`⏩ ${speeds[spdIdx]}x`; }
function toggleLoop(){ vid.loop=!vid.loop; $('loopBtn').textContent=vid.loop?'🔁 Loop: ON':'🔁 Loop: OFF'; }
function goFull(){ const s=$('stage'); !document.fullscreenElement ? (s.requestFullscreen&&s.requestFullscreen()) : (document.exitFullscreen&&document.exitFullscreen()); }

function detect(raw){
  const s=raw.trim();
  const yt=s.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/shorts\/|youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})/i);
  if(yt) return { kind:'youtube', id:yt[1], url:`https://www.youtube.com/watch?v=${yt[1]}` };
  const df=s.match(/\/folders\/([a-zA-Z0-9_-]+)/i);
  if(df) return { kind:'drive-folder', id:df[1], url:s };
  const dfi=s.match(/\/file\/d\/([a-zA-Z0-9_-]+)/i) || (s.includes('drive.google.com')&&s.match(/[?&]id=([a-zA-Z0-9_-]+)/i));
  if(dfi) return { kind:'drive-file', id:dfi[1], url:s };
  return { kind:'search', q:s };
}

function handleInput(){
  const raw=$('qInput').value.trim();
  if(!raw){ selectCat('all'); return; }
  const info=detect(raw);
  if(info.kind==='youtube') playYT(info.id, 'YouTube '+info.id);
  else if(info.kind==='drive-file') playDrive(info.id, 'Drive Video');
  else if(info.kind==='drive-folder') openDriveFolder(info.id, info.url);
  else {
    switchTab('yt');
    ['all','db','romcom','space','trading','coding'].forEach(c=>$('c-'+c).classList.remove('active'));
    feed = { mode:'search', q:info.q, wave:0, loading:false, items:[], seen:new Set() };
    $('grid').innerHTML = `<div class="empty" style="grid-column:1/-1;">🔍 Searching "${esc(info.q)}"...</div>`;
    loadNextWave(false);
  }
}

function selectCat(c){
  switchTab('yt');
  ['all','db','romcom','space','trading','coding'].forEach(x=>$('c-'+x).classList.toggle('active',x===c));
  feed = { mode:c, q:'', wave:0, loading:false, items:[], seen:new Set() };
  $('grid').innerHTML = `<div class="empty" style="grid-column:1/-1;">⏳ Loading HD Videos...</div>`;
  $('moreWrap').style.display='none';
  loadNextWave(false);
}

async function fetchSearch(q, limit, cat, noshorts){
  try{
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}&cat=${encodeURIComponent(cat)}&noshorts=${noshorts?1:0}`);
    const d = await r.json();
    return d.items || [];
  }catch(e){ return []; }
}

async function loadNextWave(append){
  if(feed.loading) return;
  feed.loading = true;
  const labels = { db:'Dragon Ball', romcom:'Rom-Com Anime', space:'Space', trading:'Trading', coding:'Coding' };
  let buckets = [];
  if(feed.mode==='all'){
    const ks=['db','romcom','space','trading','coding'];
    buckets = await Promise.all(ks.map(k => fetchSearch(WAVES[k][feed.wave % WAVES[k].length], 8, labels[k], true)));
  } else if(WAVES[feed.mode]){
    const arr = WAVES[feed.mode];
    buckets = [await fetchSearch(arr[feed.wave % arr.length], 24, labels[feed.mode], true)];
  } else {
    const suf = ['', ' HD', ' full', ' explained'];
    buckets = [await fetchSearch(feed.q + (suf[feed.wave % suf.length]||''), 24, '', false)];
  }
  const added = [];
  const mx = Math.max(0, ...buckets.map(b=>b.length));
  for(let i=0;i<mx;i++){
    for(const b of buckets){
      const it=b[i];
      if(it && !feed.seen.has(it.id)){ feed.seen.add(it.id); feed.items.push(it); added.push(it); }
    }
  }
  feed.wave++;
  feed.loading = false;
  if(!append) $('grid').innerHTML='';
  added.forEach(it=>{
    const c=document.createElement('div');
    c.className='card';
    c.innerHTML=`
      <div class="twrap">
        <img src="${esc(it.thumbHd)}" onerror="if(this.src!=='${esc(it.thumbHq)}')this.src='${esc(it.thumbHq)}';" loading="lazy">
        ${it.category?`<span class="tag">${esc(it.category)}</span>`:''}
        <span class="dur">${esc(it.duration)}</span>
      </div>
      <div class="cbody">
        <div class="ctitle">${esc(it.title)}</div>
        <div class="cmeta">${esc(it.author)}${it.views?' • '+esc(it.views):''}</div>
        <div class="cacts">
          <button class="primary">▶ Play</button>
          <button class="gold">⭐ Save</button>
        </div>
      </div>`;
    const playFn = () => playYT(it.id, it.title);
    c.querySelector('.twrap').onclick = playFn;
    c.querySelector('.ctitle').onclick = playFn;
    const bs = c.querySelectorAll('button');
    bs[0].onclick = (e)=>{ e.stopPropagation(); playFn(); };
    bs[1].onclick = (e)=>{ e.stopPropagation(); addSaved({ title:it.title, url:`https://www.youtube.com/watch?v=${it.id}`, type:'youtube', id:it.id }); };
    $('grid').appendChild(c);
  });
  $('ytCnt').textContent = feed.items.length;
  $('moreWrap').style.display = feed.items.length ? 'flex' : 'none';
  $('moreBtn').textContent = `⚡ Load More Videos (${feed.items.length} loaded)`;
}

function addSaved(item){
  saved = [{ ...item, uid:'s'+Date.now(), time:new Date().toLocaleString() }, ...saved.filter(x=>x.url!==item.url)];
  localStorage.setItem('sv_turbo_saved', JSON.stringify(saved));
  renderSaved();
  toast(`⭐ Saved "${item.title}"!`);
}
function saveCurrent(){ if(!curMedia.url){ saveInputLink(); return; } const t=prompt('Save title:',curMedia.title); if(t) addSaved({...curMedia, title:t.trim()}); }
function saveInputLink(){
  const raw=$('qInput').value.trim(); const info=detect(raw);
  if(info.kind==='search'){ toast('Paste a YouTube or Google Drive link first!'); return; }
  const t=prompt('Save name:', info.kind==='drive-folder'?'📁 Drive Folder':'🎬 Saved Video');
  if(t) addSaved({ title:t.trim(), url:info.url, type:info.kind, id:info.id });
}
function renderSaved(){
  $('svCnt').textContent = saved.length;
  const box=$('svList');
  if(!saved.length){ box.innerHTML=`<div class="empty">No saved links yet.</div>`; return; }
  box.innerHTML='';
  saved.forEach(it=>{
    const row=document.createElement('div');
    row.className='litem';
    row.innerHTML=`<div style="cursor:pointer;flex:1;"><strong>${esc(it.title)}</strong><div style="font-size:11px;color:var(--muted);">${esc(it.type.toUpperCase())} • ${esc(it.time)}</div></div>
      <div style="display:flex;gap:6px;"><button class="primary">▶ Open</button><button class="danger">🗑</button></div>`;
    const act = () => it.type==='youtube' ? playYT(it.id,it.title) : (it.type==='drive-folder' ? openDriveFolder(it.id,it.url) : playDrive(it.id,it.title));
    row.firstChild.onclick = act;
    const bs=row.querySelectorAll('button');
    bs[0].onclick = act;
    bs[1].onclick = ()=>{ saved=saved.filter(x=>x.uid!==it.uid); localStorage.setItem('sv_turbo_saved',JSON.stringify(saved)); renderSaved(); };
    box.appendChild(row);
  });
}

renderSaved();
selectCat('all');
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(HTML_PAGE)
