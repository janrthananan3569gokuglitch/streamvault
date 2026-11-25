import os
import re
import json
import time
import base64
import asyncio
from urllib.parse import quote

import httpx
import yt_dlp
from fastapi import FastAPI, Request, Query, Header
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response

app = FastAPI(title="StreamVault Turbo + Admin Studio")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
GITHUB_REPO_DEFAULT = os.environ.get("GITHUB_REPO", "janrthananan3569gokuglitch/streamvault")

COOKIE_FILE_PATH = "/tmp/yt_cookies.txt"
LIVE_HTML_PATH = "/tmp/live_index.html"
LIVE_CONFIG_PATH = "/tmp/live_config.json"

STREAM_CACHE = {}
CACHE_TTL_SEC = 1500
UA_HEADER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

DEFAULT_CONFIG = {
    "site_title": "StreamVault Turbo",
    "site_subtitle": "100% Server-Proxy YouTube + Google Drive • Zero Direct YouTube Traffic",
    "default_quality": "360",
}

# Load YouTube cookies from Render Environment Variable on startup if set
if os.environ.get("YT_COOKIES_B64"):
    try:
        raw_b64 = os.environ["YT_COOKIES_B64"].strip()
        decoded = base64.b64decode(raw_b64)
        if len(decoded) > 30:
            with open(COOKIE_FILE_PATH, "wb") as f:
                f.write(decoded)
    except Exception:
        pass


def b64u_encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> str:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii")).decode("utf-8", errors="ignore")


def has_valid_cookies() -> bool:
    return os.path.isfile(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 30


def read_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(LIVE_CONFIG_PATH):
        try:
            with open(LIVE_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    cfg.update(data)
        except Exception:
            pass
    return cfg


def write_config(cfg: dict):
    with open(LIVE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_live_html() -> str:
    if os.path.isfile(LIVE_HTML_PATH):
        try:
            with open(LIVE_HTML_PATH, "r", encoding="utf-8") as f:
                txt = f.read()
                if txt.strip():
                    return txt
        except Exception:
            pass
    repo_html = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.isfile(repo_html):
        try:
            with open(repo_html, "r", encoding="utf-8") as f:
                txt = f.read()
                if txt.strip():
                    return txt
        except Exception:
            pass
    return DEFAULT_HTML_PAGE


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
    if has_valid_cookies():
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


def _extract_via_external_mirrors(video_id: str, quality: str = "360"):
    target_h = 360 if quality == "360" else 720
    with httpx.Client(follow_redirects=True, timeout=5.5, headers={"User-Agent": UA_HEADER}) as client:
        # Layer A: Piped API Cluster (Direct combined MP4 streams)
        piped_hosts = [
            "https://pipedapi.kavin.rocks",
            "https://api.piped.private.coffee",
            "https://pipedapi.adminforge.de",
            "https://pipedapi.leptons.xyz",
        ]
        for host in piped_hosts:
            try:
                r = client.get(f"{host}/streams/{video_id}")
                if r.status_code != 200:
                    continue
                d = r.json()
                streams = [s for s in (d.get("videoStreams") or []) if not s.get("videoOnly") and s.get("url")]
                if streams:
                    streams.sort(key=lambda s: abs(int(s.get("height") or 360) - target_h))
                    chosen = streams[0]
                    return {
                        "url": chosen["url"],
                        "headers": {"User-Agent": UA_HEADER},
                        "title": str(d.get("title") or f"Video {video_id}"),
                        "height": int(chosen.get("height") or target_h),
                        "engine": "PIPED SERVER PROXY",
                        "ts": time.time(),
                    }
            except Exception:
                continue

        # Layer B: Invidious API Cluster (local=true avoids IP-lock)
        inv_hosts = [
            "https://invidious.nerdvpn.de",
            "https://yewtu.be",
            "https://iv.datura.network",
            "https://invidious.protokolla.fi",
        ]
        for host in inv_hosts:
            try:
                r = client.get(f"{host}/api/v1/videos/{video_id}?local=true")
                if r.status_code != 200:
                    continue
                d = r.json()
                fstreams = [f for f in (d.get("formatStreams") or []) if f.get("url")]
                if fstreams:
                    chosen = fstreams[0]
                    for fs in fstreams:
                        ql = str(fs.get("qualityLabel") or "")
                        if str(target_h) in ql:
                            chosen = fs
                            break
                    u = chosen["url"]
                    if u.startswith("/"):
                        u = host + u
                    return {
                        "url": u,
                        "headers": {"User-Agent": UA_HEADER},
                        "title": str(d.get("title") or f"Video {video_id}"),
                        "height": target_h,
                        "engine": "INVIDIOUS PROXY",
                        "ts": time.time(),
                    }
            except Exception:
                continue

        # Layer C: Cobalt Extractor Nodes
        cobalt_hosts = [
            "https://api.cobalt.tools/",
            "https://cobalt-api.kwiatekmiki.com/",
        ]
        for chost in cobalt_hosts:
            try:
                r = client.post(
                    chost,
                    json={"url": f"https://www.youtube.com/watch?v={video_id}", "videoQuality": str(target_h)},
                    headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": UA_HEADER},
                )
                if r.status_code == 200:
                    d = r.json()
                    if d.get("url"):
                        return {
                            "url": d["url"],
                            "headers": {"User-Agent": UA_HEADER},
                            "title": str(d.get("filename") or f"Video {video_id}"),
                            "height": target_h,
                            "engine": "COBALT SERVER PROXY",
                            "ts": time.time(),
                        }
            except Exception:
                continue
    return None


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

    last_err = "YouTube bot-check blocked datacenter IP."
    cookie_ready = has_valid_cookies()

    # 1. If Cookies are configured, run yt-dlp with cookies first (100% Reliable!)
    if cookie_ready:
        client_sets = [["web", "ios"], ["android_vr", "tv_embedded"], ["mweb"]]
    else:
        client_sets = [["android_vr"], ["tv_embedded"], ["ios"], ["mweb"]]

    for clients in client_sets:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": fmt,
            "socket_timeout": 7,
            "extractor_args": {"youtube": {"player_client": clients}},
        }
        if cookie_ready:
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
                        "engine": "COOKIES PROXY" if cookie_ready else f"YT {clients[0].upper()}",
                        "ts": time.time(),
                    }
                    STREAM_CACHE[cache_key] = res
                    return res
        except Exception as exc:
            last_err = str(exc)

    # 2. Automatic Server-Side Mirror Fallback (Piped / Invidious / Cobalt)
    mirror_res = _extract_via_external_mirrors(video_id, quality)
    if mirror_res:
        STREAM_CACHE[cache_key] = mirror_res
        return mirror_res

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


# =========================================================================
# PUBLIC VIDEO & PROXY ROUTES
# =========================================================================
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


@app.get("/api/config")
async def api_public_config():
    cfg = read_config()
    cfg["cookies_active"] = has_valid_cookies()
    return JSONResponse(cfg)


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
            "engine": data.get("engine", "PROXY"),
            "streamUrl": f"/stream/{vid}?q={quality}",
            "thumb": f"/img?u={b64u_encode(f'https://i.ytimg.com/vi/{vid}/hq720.jpg')}",
        })
    except Exception as exc:
        msg = str(exc)
        needs_cookies = ("confirm you" in msg.lower()) or ("bot" in msg.lower()) or ("cookies" in msg.lower())
        return JSONResponse({
            "status": "error",
            "needsCookies": needs_cookies,
            "message": msg[:260],
        }, status_code=500)


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


# =========================================================================
# ADMIN API ROUTES (LIVE CODE EDITOR + COOKIES FIXER + GITHUB PUSH)
# =========================================================================
def _check_admin(pwd: str):
    return str(pwd or "").strip() == str(ADMIN_PASSWORD).strip()


@app.get("/api/admin/state")
async def api_admin_state(x_admin_pass: str = Header("")):
    if not _check_admin(x_admin_pass):
        return JSONResponse({"status": "error", "message": "Wrong Admin Password"}, status_code=401)
    cookie_preview = ""
    if has_valid_cookies():
        try:
            with open(COOKIE_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                cookie_preview = f.read()
        except Exception:
            pass
    app_py_text = ""
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            app_py_text = f.read()
    except Exception:
        pass
    return JSONResponse({
        "status": "ok",
        "html": get_live_html(),
        "app_py": app_py_text,
        "config": read_config(),
        "cookies_active": has_valid_cookies(),
        "cookies_text": cookie_preview,
        "cache_size": len(STREAM_CACHE),
        "ytdlp_version": getattr(yt_dlp.version, "__version__", "unknown"),
        "github_repo": GITHUB_REPO_DEFAULT,
    })


@app.post("/api/admin/save_html")
async def api_admin_save_html(request: Request, x_admin_pass: str = Header("")):
    if not _check_admin(x_admin_pass):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=401)
    body = await request.json()
    html = str(body.get("html") or "")
    if len(html.strip()) < 50:
        return JSONResponse({"status": "error", "message": "HTML content too short"}, status_code=400)
    with open(LIVE_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    return JSONResponse({"status": "ok", "message": "Website updated LIVE instantly!"})


@app.post("/api/admin/reset_html")
async def api_admin_reset_html(x_admin_pass: str = Header("")):
    if not _check_admin(x_admin_pass):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=401)
    if os.path.isfile(LIVE_HTML_PATH):
        os.remove(LIVE_HTML_PATH)
    return JSONResponse({"status": "ok", "html": get_live_html(), "message": "Reset to default website code!"})


@app.post("/api/admin/save_cookies")
async def api_admin_save_cookies(request: Request, x_admin_pass: str = Header("")):
    if not _check_admin(x_admin_pass):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=401)
    body = await request.json()
    raw = str(body.get("cookies") or "").strip()
    if not raw:
        if os.path.isfile(COOKIE_FILE_PATH):
            os.remove(COOKIE_FILE_PATH)
        STREAM_CACHE.clear()
        return JSONResponse({"status": "ok", "cookies_active": False, "b64": "", "message": "Cookies cleared."})

    # Accept either raw Netscape cookies.txt OR Base64 string
    if "youtube.com" not in raw and not raw.startswith("#"):
        try:
            decoded = base64.b64decode(raw).decode("utf-8", errors="ignore")
            if "youtube.com" in decoded or decoded.startswith("#"):
                raw = decoded
        except Exception:
            pass

    with open(COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(raw + "\n")
    STREAM_CACHE.clear()
    b64_val = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return JSONResponse({
        "status": "ok",
        "cookies_active": True,
        "b64": b64_val,
        "message": "YouTube Cookies activated LIVE! Bot-check fixed!",
    })


@app.post("/api/admin/save_config")
async def api_admin_save_config(request: Request, x_admin_pass: str = Header("")):
    if not _check_admin(x_admin_pass):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=401)
    body = await request.json()
    cfg = read_config()
    if "site_title" in body:
        cfg["site_title"] = str(body["site_title"]).strip() or DEFAULT_CONFIG["site_title"]
    if "site_subtitle" in body:
        cfg["site_subtitle"] = str(body["site_subtitle"]).strip() or DEFAULT_CONFIG["site_subtitle"]
    if "default_quality" in body:
        cfg["default_quality"] = "720" if str(body["default_quality"]) == "720" else "360"
    write_config(cfg)
    return JSONResponse({"status": "ok", "config": cfg, "message": "Site settings updated LIVE!"})


@app.post("/api/admin/github_push")
async def api_admin_github_push(request: Request, x_admin_pass: str = Header("")):
    if not _check_admin(x_admin_pass):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=401)
    body = await request.json()
    token = str(body.get("token") or os.environ.get("GITHUB_TOKEN") or "").strip()
    repo = str(body.get("repo") or GITHUB_REPO_DEFAULT).strip()
    filepath = str(body.get("filepath") or "index.html").strip().lstrip("/")
    content = str(body.get("content") or "")
    commit_msg = str(body.get("message") or f"Admin Studio live update: {filepath}")

    if not token:
        return JSONResponse({"status": "error", "message": "Enter a GitHub Personal Access Token (Contents: Read & Write) to push permanently."}, status_code=400)
    if filepath not in ("index.html", "app.py", "Dockerfile"):
        return JSONResponse({"status": "error", "message": "Allowed files: index.html, app.py, Dockerfile"}, status_code=400)

    api_url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "StreamVault-Admin",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            sha = None
            get_r = await client.get(api_url, headers=headers)
            if get_r.status_code == 200:
                sha = get_r.json().get("sha")
            payload = {
                "message": commit_msg,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            }
            if sha:
                payload["sha"] = sha
            put_r = await client.put(api_url, headers=headers, json=payload)
            if put_r.status_code in (200, 201):
                return JSONResponse({"status": "ok", "message": f"Pushed {filepath} to GitHub! Render will auto-deploy in 1-2 mins."})
            return JSONResponse({"status": "error", "message": f"GitHub Error {put_r.status_code}: {put_r.text[:220]}"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


# =========================================================================
# DEFAULT FRONTEND WEBSITE HTML (EDITABLE LIVE FROM /admin)
# =========================================================================
DEFAULT_HTML_PAGE = r"""<!doctype html>
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
.top-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.badge{
  padding:7px 12px;border-radius:999px;font-size:11.5px;font-weight:900;
  background:linear-gradient(135deg,#10b981,#00f5d4);color:#031915;
}
.admin-link{
  text-decoration:none;padding:7px 13px;border-radius:999px;font-size:11.5px;font-weight:900;
  background:linear-gradient(135deg,#fbbf24,#fb923c);color:#1c0f00;
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
#vid,#embedFrame{width:100%;height:100%;background:#000;border:0;display:none;}
#vid.active,#embedFrame.active{display:block;}
#vid{object-fit:contain;}
.placeholder{text-align:center;padding:24px;color:var(--muted);max-width:520px;}
.placeholder strong{display:block;color:#fff;font-size:16px;margin-bottom:6px;}
.loader{
  position:absolute;inset:0;background:rgba(5,8,18,0.88);display:none;
  flex-direction:column;align-items:center;justify-content:center;gap:10px;z-index:5;
}
.loader.show{display:flex;}
.spin{width:38px;height:38px;border-radius:50%;border:3px solid #1e335c;border-top-color:var(--cyan);animation:sp 0.7s linear infinite;}
@keyframes sp{to{transform:rotate(360deg);}}
.err-banner{
  display:none;padding:11px 14px;background:#2d0e1f;border-top:1px solid #ff2e7e;
  color:#fecdd3;font-size:12.5px;font-weight:700;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;
}
.err-banner.show{display:flex;}
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
.toast-box{position:fixed;bottom:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:7px;max-width:92vw;}
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
        <h1 id="siteTitleEl">StreamVault Turbo</h1>
        <small id="siteSubEl">100% Server-Proxy YouTube + Google Drive • Zero Direct YouTube Traffic</small>
      </div>
    </div>
    <div class="top-right">
      <span class="badge" id="cookieStatusBadge">🛡️ PROXY ENGINE</span>
      <a class="admin-link" href="/admin">⚙️ Admin Panel</a>
    </div>
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
        <p>Every search, thumbnail, and video stream comes through this server — fast 360p College Mode or 720p HD!</p>
      </div>
      <video id="vid" controls playsinline preload="auto"></video>
      <iframe id="embedFrame" allow="autoplay; fullscreen; encrypted-media" allowfullscreen></iframe>
    </div>
    <div class="err-banner" id="errBanner">
      <span id="errText">YouTube requested bot verification on this cloud IP.</span>
      <div style="display:flex;gap:7px;flex-wrap:wrap;">
        <a href="/admin" style="text-decoration:none;"><button class="gold" type="button">🍪 Fix in Admin Panel</button></a>
        <button class="primary" type="button" onclick="playEmbedFallback()">📺 Try Embed Player</button>
      </div>
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
        <button class="pink" id="q360Btn" onclick="setQuality('360')">⚡ 360p Fast</button>
        <button id="q720Btn" onclick="setQuality('720')">🎬 720p HD</button>
        <button onclick="playEmbedFallback()" id="embBtn">📺 Embed Mode</button>
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
const emb = $('embedFrame');
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
function toast(m){const d=document.createElement('div');d.className='toast';d.textContent=m;$('toasts').appendChild(d);setTimeout(()=>d.remove(),3200);}
function setLoad(on,t){$('loader').classList.toggle('show',on);if(t)$('loadTxt').textContent=t;}
function switchTab(t){['yt','drive','saved'].forEach(x=>{$('tb-'+x).classList.toggle('active',x===t);$('pn-'+x).classList.toggle('active',x===t);});}

function clearPlayers(){
  $('errBanner').classList.remove('show');
  try{ vid.pause(); vid.removeAttribute('src'); vid.load(); }catch(e){}
  vid.classList.remove('active');
  emb.src = 'about:blank';
  emb.classList.remove('active');
}

function setQuality(q){
  quality = q;
  $('q360Btn').className = q==='360' ? 'pink' : '';
  $('q720Btn').className = q==='720' ? 'pink' : '';
  toast(q==='360' ? '⚡ 360p Fast College Mode!' : '🎬 720p HD Mode!');
  if(curMedia.type==='youtube' && curMedia.id) playYT(curMedia.id, curMedia.title);
}

function playEmbedFallback(){
  if(!curMedia.id) return;
  clearPlayers();
  $('ph').style.display='none';
  setLoad(false);
  if(curMedia.type==='drive-file'){
    emb.src = `https://drive.google.com/file/d/${encodeURIComponent(curMedia.id)}/preview`;
    $('pBadge').textContent = 'DRIVE HD EMBED';
  } else {
    emb.src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(curMedia.id)}?autoplay=1&rel=0&playsinline=1`;
    $('pBadge').textContent = 'YOUTUBE EMBED';
  }
  emb.classList.add('active');
}

async function playYT(id, title){
  clearPlayers();
  $('ph').style.display='none';
  setLoad(true, `Preparing ${quality}p Server-Proxy Stream...`);
  curMedia = { type:'youtube', id, title:title||id, url:`https://www.youtube.com/watch?v=${id}` };
  $('nowTitle').textContent = `▶ ${curMedia.title}`;
  $('saveInfo').textContent = `🎬 YouTube: ${curMedia.title}`;
  $('pCard').scrollIntoView({behavior:'smooth', block:'start'});
  try{
    const r = await fetch(`/api/resolve?id=${encodeURIComponent(id)}&q=${quality}`);
    const d = await r.json();
    if(d.status==='ok' && d.streamUrl){
      if(d.title){ curMedia.title = d.title; $('nowTitle').textContent = `▶ ${d.title}`; }
      $('pBadge').textContent = `${d.engine || 'PROXY'} ${d.height||quality}P`;
      vid.classList.add('active');
      vid.poster = d.thumb || '';
      vid.src = d.streamUrl;
      vid.load();
      vid.play().catch(()=>{});
    } else {
      setLoad(false);
      $('errText').textContent = d.needsCookies
        ? '🍪 YouTube Bot-Check detected! Open Admin Panel and paste YouTube cookies.txt once to fix 100%.'
        : ('⚠️ ' + (d.message || 'Stream unavailable.'));
      $('errBanner').classList.add('show');
    }
  }catch(e){
    setLoad(false);
    toast('⚠️ Connection error: ' + e.message);
  }
}

function playDrive(id, title){
  clearPlayers();
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
vid.addEventListener('error', ()=>{
  setLoad(false);
  if(curMedia.type==='drive-file'){ playEmbedFallback(); return; }
  $('errBanner').classList.add('show');
});

function togglePlay(){ if(vid.classList.contains('active') && vid.src) vid.paused ? vid.play().catch(()=>{}) : vid.pause(); }
function seek(s){ if(vid.classList.contains('active') && vid.src) vid.currentTime = Math.max(0, Math.min(vid.duration||99999, (vid.currentTime||0)+s)); }
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

fetch('/api/config').then(r=>r.json()).then(c=>{
  if(c.site_title){ $('siteTitleEl').textContent = c.site_title; document.title = c.site_title; }
  if(c.site_subtitle) $('siteSubEl').textContent = c.site_subtitle;
  if(c.default_quality){ quality = c.default_quality; $('q360Btn').className = quality==='360'?'pink':''; $('q720Btn').className = quality==='720'?'pink':''; }
  $('cookieStatusBadge').textContent = c.cookies_active ? '🟢 COOKIES ACTIVE' : '🛡️ PROXY ENGINE';
}).catch(()=>{});

renderSaved();
selectCat('all');
</script>
</body>
</html>
"""


# =========================================================================
# BUILT-IN LIVE ADMIN PANEL (/admin)
# =========================================================================
ADMIN_HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5">
<title>StreamVault Live Admin Studio</title>
<style>
:root{--bg:#050712;--surf:#0e1428;--card:#131b36;--cyan:#00f5d4;--blue:#38bdf8;--gold:#fbbf24;--pink:#ff2e7e;--text:#f8fafc;--muted:#94a3b8;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;padding:14px;min-height:100vh;}
.wrap{max-width:1180px;margin:0 auto;}
.top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;padding:13px 16px;background:var(--surf);border:1px solid #284070;border-radius:15px;margin-bottom:14px;}
.top h1{font-size:18px;font-weight:900;background:linear-gradient(90deg,#00f5d4,#fbbf24,#ff2e7e);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
button,a.btn{font:inherit;text-decoration:none;cursor:pointer;border-radius:10px;padding:9px 14px;font-size:12.5px;font-weight:800;border:1px solid #2c4375;background:#182447;color:#fff;display:inline-flex;align-items:center;gap:6px;}
button.primary,a.primary{background:linear-gradient(135deg,#00f5d4,#38bdf8);color:#04101d;border:none;}
button.gold{background:linear-gradient(135deg,#fbbf24,#fb923c);color:#1c0f00;border:none;}
button.pink{background:linear-gradient(135deg,#ff2e7e,#a855f7);color:#fff;border:none;}
button.danger{background:#3b1224;border-color:#ff2e7e;color:#fecdd3;}
.card{background:var(--surf);border:1px solid #243661;border-radius:15px;padding:15px;margin-bottom:14px;}
.login-box{max-width:420px;margin:60px auto;text-align:center;}
input,textarea,select{width:100%;background:#060916;color:#00f5d4;border:1.5px solid #2b4275;border-radius:11px;padding:11px 13px;font-size:13px;outline:none;margin-top:6px;}
textarea.code{height:440px;font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre;overflow:auto;tab-size:2;}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;}
.tab{flex:1;min-width:140px;padding:10px;background:var(--card);}
.tab.active{background:linear-gradient(135deg,#00f5d4,#38bdf8);color:#04101d;border:none;}
.pane{display:none;}.pane.active{display:block;}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px;}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;}
.pill{padding:6px 11px;border-radius:999px;font-size:11.5px;font-weight:800;background:#132042;border:1px solid #2b447c;}
.pill.ok{background:rgba(16,185,129,0.2);border-color:#10b981;color:#6ee7b7;}
.pill.warn{background:rgba(255,46,126,0.2);border-color:#ff2e7e;color:#fecdd3;}
.msg{padding:10px 13px;border-radius:10px;margin-bottom:12px;font-size:12.5px;font-weight:700;display:none;background:#10293d;border:1px solid var(--cyan);}
.msg.show{display:block;}
label{display:block;font-size:12px;font-weight:800;color:#cbd5e1;margin-top:10px;}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1>⚙️ StreamVault Live Admin Studio</h1>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <a class="btn primary" href="/" target="_blank">🌐 Open Live Website</a>
      <button class="danger" id="logoutBtn" style="display:none;" onclick="logout()">🚪 Logout</button>
    </div>
  </div>

  <div id="msgBox" class="msg"></div>

  <!-- LOGIN VIEW -->
  <div id="loginView" class="card login-box">
    <h2 style="margin-bottom:8px;">🔐 Admin Login</h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:12px;">Default password: <b>admin123</b> (Change anytime via Render Env: ADMIN_PASSWORD)</p>
    <input type="password" id="passInp" placeholder="Enter Admin Password..." onkeydown="if(event.key==='Enter')doLogin()">
    <button class="primary" style="width:100%;margin-top:12px;justify-content:center;" onclick="doLogin()">🔓 Unlock Admin Studio</button>
  </div>

  <!-- ADMIN WORKSPACE -->
  <div id="adminView" style="display:none;">
    <div class="stats">
      <span class="pill" id="stCookies">Cookies: Checking...</span>
      <span class="pill" id="stYtdlp">yt-dlp: ...</span>
      <span class="pill" id="stCache">Stream Cache: 0</span>
    </div>

    <div class="tabs">
      <button class="tab active" id="t-cookies" onclick="openTab('cookies')">🍪 1. Fix YouTube Bot-Check</button>
      <button class="tab" id="t-html" onclick="openTab('html')">💻 2. Live Website Code</button>
      <button class="tab" id="t-settings" onclick="openTab('settings')">⚙️ 3. Site Settings</button>
      <button class="tab" id="t-github" onclick="openTab('github')">🚀 4. Permanent GitHub Push</button>
    </div>

    <!-- TAB 1: YOUTUBE COOKIES FIXER -->
    <div id="p-cookies" class="pane active card">
      <h3 style="color:var(--gold);margin-bottom:6px;">🍪 Fix "Sign in to confirm you're not a bot" (100% Working Fix)</h3>
      <p style="font-size:12.5px;color:#cbd5e1;line-height:1.5;">
        Paste your YouTube <b>cookies.txt</b> text below and click <b>Activate Cookies Now</b>. Videos will start playing immediately on your live website!
      </p>
      <label>Paste YouTube cookies.txt (Netscape format or Base64):</label>
      <textarea id="cookiesArea" class="code" style="height:220px;" placeholder="# Netscape HTTP Cookie File&#10;.youtube.com	TRUE	/	TRUE	..."></textarea>
      <div class="row">
        <button class="primary" onclick="saveCookies()">✅ Activate Cookies Now (Instant Fix)</button>
        <button class="danger" onclick="clearCookies()">🗑 Clear Cookies</button>
      </div>
      <div id="b64Wrap" style="display:none;margin-top:14px;">
        <label>📋 Optional — Copy this Base64 into Render Dashboard → Environment → Key: <code>YT_COOKIES_B64</code> (so cookies stay active even after Render restarts):</label>
        <textarea id="b64Out" style="height:85px;font:11px monospace;color:#fbbf24;" readonly></textarea>
        <div class="row"><button class="gold" onclick="copyB64()">📋 Copy YT_COOKIES_B64 Value</button></div>
      </div>
    </div>

    <!-- TAB 2: LIVE WEBSITE CODE EDITOR -->
    <div id="p-html" class="pane card">
      <h3 style="color:var(--cyan);margin-bottom:6px;">💻 Live Website Code Editor (HTML + CSS + JS)</h3>
      <p style="font-size:12px;color:var(--muted);">Edit anything below and click <b>Save & Update Live</b> — your website updates immediately without restarting Render!</p>
      <textarea id="htmlEditor" class="code"></textarea>
      <div class="row">
        <button class="primary" onclick="saveLiveHtml()">💾 Save & Update Live Website</button>
        <button class="gold" onclick="pushFileToGithub('index.html', document.getElementById('htmlEditor').value)">🚀 Save Permanently to GitHub</button>
        <button class="danger" onclick="resetHtml()">🔄 Reset to Default</button>
      </div>
    </div>

    <!-- TAB 3: QUICK SITE SETTINGS -->
    <div id="p-settings" class="pane card">
      <h3 style="color:var(--blue);margin-bottom:6px;">⚙️ Quick Live Website Settings</h3>
      <label>Website Title:</label>
      <input id="cfgTitle" placeholder="StreamVault Turbo">
      <label>Website Subtitle:</label>
      <input id="cfgSub" placeholder="100% Server-Proxy YouTube + Google Drive">
      <label>Default Video Quality:</label>
      <select id="cfgQual">
        <option value="360">⚡ 360p Fast (Best for College Wi-Fi)</option>
        <option value="720">🎬 720p HD</option>
      </select>
      <div class="row">
        <button class="primary" onclick="saveConfig()">💾 Apply Settings Live</button>
      </div>
    </div>

    <!-- TAB 4: PERMANENT GITHUB SYNC (HTML & APP.PY) -->
    <div id="p-github" class="pane card">
      <h3 style="color:var(--pink);margin-bottom:6px;">🚀 Permanent GitHub Auto-Deploy (Survives Render Restarts)</h3>
      <p style="font-size:12px;color:#cbd5e1;line-height:1.5;">
        Render Free resets temporary files when sleeping. Enter your GitHub Repo & Token once to push <b>index.html</b> or <b>app.py</b> directly to GitHub so Render auto-deploys permanently!
      </p>
      <label>GitHub Repository (username/repo):</label>
      <input id="ghRepo" placeholder="janrthananan3569gokuglitch/streamvault">
      <label>GitHub Personal Access Token (stored in your browser only):</label>
      <input type="password" id="ghToken" placeholder="ghp_xxxxxxxxxxxxxxxxxxxx">
      <label>Backend Python Code (app.py):</label>
      <textarea id="appPyEditor" class="code" style="height:280px;"></textarea>
      <div class="row">
        <button class="pink" onclick="pushFileToGithub('app.py', document.getElementById('appPyEditor').value)">🚀 Push app.py to GitHub</button>
        <button class="gold" onclick="pushFileToGithub('index.html', document.getElementById('htmlEditor').value)">🚀 Push Live HTML (index.html) to GitHub</button>
      </div>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let adminPass = sessionStorage.getItem('sv_admin_pass') || '';

function showMsg(t){ const b=$('msgBox'); b.textContent=t; b.classList.add('show'); setTimeout(()=>b.classList.remove('show'), 5000); }
function openTab(t){ ['cookies','html','settings','github'].forEach(x=>{$('t-'+x).classList.toggle('active',x===t);$('p-'+x).classList.toggle('active',x===t);}); }

async function loadState(){
  const r = await fetch('/api/admin/state', { headers: { 'X-Admin-Pass': adminPass } });
  const d = await r.json();
  if(r.status === 401 || d.status !== 'ok'){
    sessionStorage.removeItem('sv_admin_pass');
    $('loginView').style.display='block';
    $('adminView').style.display='none';
    $('logoutBtn').style.display='none';
    if(adminPass) showMsg('❌ Wrong Admin Password');
    return;
  }
  sessionStorage.setItem('sv_admin_pass', adminPass);
  $('loginView').style.display='none';
  $('adminView').style.display='block';
  $('logoutBtn').style.display='inline-flex';
  $('htmlEditor').value = d.html || '';
  $('appPyEditor').value = d.app_py || '';
  $('cookiesArea').value = d.cookies_text || '';
  $('cfgTitle').value = (d.config && d.config.site_title) || '';
  $('cfgSub').value = (d.config && d.config.site_subtitle) || '';
  $('cfgQual').value = (d.config && d.config.default_quality) || '360';
  $('ghRepo').value = localStorage.getItem('sv_gh_repo') || d.github_repo || '';
  $('ghToken').value = localStorage.getItem('sv_gh_token') || '';

  $('stCookies').textContent = d.cookies_active ? '🟢 YouTube Cookies: ACTIVE' : '🔴 YouTube Cookies: NOT SET';
  $('stCookies').className = 'pill ' + (d.cookies_active ? 'ok' : 'warn');
  $('stYtdlp').textContent = 'yt-dlp: ' + (d.ytdlp_version || '');
  $('stCache').textContent = 'Cached Streams: ' + (d.cache_size || 0);
}

function doLogin(){ adminPass = $('passInp').value.trim(); loadState(); }
function logout(){ sessionStorage.removeItem('sv_admin_pass'); adminPass=''; location.reload(); }

async function saveLiveHtml(){
  const r = await fetch('/api/admin/save_html', {
    method:'POST',
    headers:{ 'Content-Type':'application/json', 'X-Admin-Pass': adminPass },
    body: JSON.stringify({ html: $('htmlEditor').value })
  });
  const d = await r.json();
  showMsg(d.status==='ok' ? '✅ ' + d.message : '❌ ' + d.message);
}

async function resetHtml(){
  if(!confirm('Reset live website HTML to default?')) return;
  const r = await fetch('/api/admin/reset_html', { method:'POST', headers:{ 'X-Admin-Pass': adminPass } });
  const d = await r.json();
  if(d.html) $('htmlEditor').value = d.html;
  showMsg('🔄 ' + (d.message || 'Reset done'));
}

async function saveCookies(){
  const r = await fetch('/api/admin/save_cookies', {
    method:'POST',
    headers:{ 'Content-Type':'application/json', 'X-Admin-Pass': adminPass },
    body: JSON.stringify({ cookies: $('cookiesArea').value })
  });
  const d = await r.json();
  showMsg(d.status==='ok' ? '✅ ' + d.message : '❌ ' + d.message);
  if(d.b64){ $('b64Wrap').style.display='block'; $('b64Out').value = d.b64; }
  loadState();
}

async function clearCookies(){
  $('cookiesArea').value = '';
  await saveCookies();
  $('b64Wrap').style.display='none';
}

function copyB64(){ navigator.clipboard.writeText($('b64Out').value).then(()=>showMsg('📋 Copied YT_COOKIES_B64! Paste it in Render Environment Variables.')); }

async function saveConfig(){
  const r = await fetch('/api/admin/save_config', {
    method:'POST',
    headers:{ 'Content-Type':'application/json', 'X-Admin-Pass': adminPass },
    body: JSON.stringify({ site_title: $('cfgTitle').value, site_subtitle: $('cfgSub').value, default_quality: $('cfgQual').value })
  });
  const d = await r.json();
  showMsg('✅ ' + (d.message || 'Saved'));
}

async function pushFileToGithub(filepath, content){
  const repo = $('ghRepo').value.trim();
  const token = $('ghToken').value.trim();
  if(!token){ openTab('github'); showMsg('⚠️ Enter your GitHub Token in Tab 4 first!'); return; }
  localStorage.setItem('sv_gh_repo', repo);
  localStorage.setItem('sv_gh_token', token);
  showMsg(`⏳ Pushing ${filepath} to GitHub...`);
  const r = await fetch('/api/admin/github_push', {
    method:'POST',
    headers:{ 'Content-Type':'application/json', 'X-Admin-Pass': adminPass },
    body: JSON.stringify({ repo, token, filepath, content })
  });
  const d = await r.json();
  showMsg(d.status==='ok' ? '🚀 ' + d.message : '❌ ' + d.message);
}

if(adminPass) loadState();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(get_live_html())


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(ADMIN_HTML_PAGE)
