from flask import Flask, render_template, request, send_file, jsonify, after_this_request
import yt_dlp
import tempfile
import shutil
import os
import glob
import re
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

TIKTOK_HOST_RE = re.compile(r"(^|\.)tiktok\.com$", re.I)
POST_ID_RE = re.compile(r"/(?:video|photo)/(\d+)")
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)


def is_tiktok_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return bool(TIKTOK_HOST_RE.search(host)) or host in {"vm.tiktok.com", "vt.tiktok.com"}
    except Exception:
        return False


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept": "*/*",
    })
    return s


def resolve_tiktok_url(url: str, session: requests.Session):
    r = session.get(url, allow_redirects=True, timeout=20)
    r.raise_for_status()
    return r.url, r.text


def post_id_from_url(url: str):
    m = POST_ID_RE.search(url)
    return m.group(1) if m else None


def recursive_find_item(obj, post_id):
    """Find a TikTok item dict inside TikTok's embedded page JSON."""
    if isinstance(obj, dict):
        obj_id = str(obj.get("id", ""))
        if obj_id == str(post_id) and ("imagePost" in obj or "video" in obj):
            return obj

        # Common TikTok state shape: ItemModule: {POST_ID: {...}}
        module = obj.get("ItemModule")
        if isinstance(module, dict):
            candidate = module.get(str(post_id))
            if isinstance(candidate, dict):
                return candidate

        for value in obj.values():
            found = recursive_find_item(value, post_id)
            if found:
                return found

    elif isinstance(obj, list):
        for value in obj:
            found = recursive_find_item(value, post_id)
            if found:
                return found
    return None


def item_from_html(html, post_id):
    # TikTok has used several embedded JSON containers over time.
    patterns = [
        r'<script[^>]+id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>',
        r'<script[^>]+id=["\']SIGI_STATE["\'][^>]*>(.*?)</script>',
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.I | re.S):
            raw = match.group(1).strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            found = recursive_find_item(data, post_id)
            if found:
                return found
    return None


def get_tiktok_item(final_url, html, post_id, session):
    # First try the same JSON endpoint used by TikTok's web experience.
    api_url = "https://www.tiktok.com/api/item/detail/"
    try:
        r = session.get(
            api_url,
            params={"itemId": post_id},
            headers={"Referer": final_url},
            timeout=20,
        )
        if r.ok:
            data = r.json()
            candidate = (
                data.get("itemInfo", {}).get("itemStruct")
                or data.get("itemStruct")
            )
            if isinstance(candidate, dict):
                return candidate
    except Exception:
        pass

    # If TikTok rejects the endpoint, use the JSON already embedded in the page.
    return item_from_html(html, post_id)


def best_image_url(image):
    image_url = image.get("imageURL") or image.get("imageUrl") or {}
    urls = image_url.get("urlList") or image_url.get("url_list") or []
    if isinstance(urls, str):
        urls = [urls]

    # Prefer formats FFmpeg commonly handles.
    for u in urls:
        if isinstance(u, str) and u.startswith("http") and ".heic" not in u.lower():
            return u
    for u in urls:
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


def download_binary(session, url, destination, referer):
    with session.get(
        url,
        headers={"Referer": referer},
        stream=True,
        timeout=30,
    ) as r:
        r.raise_for_status()
        with open(destination, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


def render_slideshow(item, final_url, temp_dir, session):
    image_post = item.get("imagePost") or {}
    images = image_post.get("images") or []
    image_urls = [best_image_url(img) for img in images if isinstance(img, dict)]
    image_urls = [u for u in image_urls if u]

    if not image_urls:
        raise RuntimeError("TikTok n'a pas fourni les images de ce slideshow.")

    music = item.get("music") or {}
    music_url = music.get("playUrl") or music.get("play_url")
    if not music_url:
        raise RuntimeError("TikTok n'a pas fourni la musique de ce slideshow.")

    # Limit protects the small free server from pathological posts.
    image_urls = image_urls[:35]

    image_files = []
    for i, image_url in enumerate(image_urls):
        path = os.path.join(temp_dir, f"slide_{i:03d}.img")
        download_binary(session, image_url, path, final_url)
        image_files.append(path)

    audio_path = os.path.join(temp_dir, "music.audio")
    download_binary(session, music_url, audio_path, final_url)

    # TikTok photo mode has no fixed automatic viewing time because users swipe.
    # 2.5 s/image makes a conventional video export while preserving all slides.
    seconds_per_slide = 2.5
    concat_path = os.path.join(temp_dir, "slides.txt")

    def ffconcat_escape(path):
        return path.replace("\\", "\\\\").replace("'", "'\\''")

    with open(concat_path, "w", encoding="utf-8") as f:
        for p in image_files:
            f.write(f"file '{ffconcat_escape(p)}'\n")
            f.write(f"duration {seconds_per_slide}\n")
        # FFmpeg concat demuxer needs the final frame repeated.
        f.write(f"file '{ffconcat_escape(image_files[-1])}'\n")

    output = os.path.join(temp_dir, "slideshow.mp4")
    total_duration = len(image_files) * seconds_per_slide

    vf = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
        "format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_path,
        "-i", audio_path,
        "-t", f"{total_duration:.2f}",
        "-vf", vf,
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        output,
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )
    if result.returncode != 0 or not os.path.exists(output):
        err = (result.stderr or "")[-800:]
        raise RuntimeError(f"FFmpeg n'a pas pu crÃ©er la vidÃ©o. {err}")

    return output, len(image_files)


def download_normal_video(url, temp_dir):
    outtmpl = os.path.join(temp_dir, "%(id)s.%(ext)s")
    opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [
        p for p in glob.glob(os.path.join(temp_dir, "*"))
        if os.path.isfile(p)
        and not p.endswith(".part")
        and not p.endswith(".txt")
        and Path(p).name != "music.audio"
    ]
    if not files:
        raise RuntimeError("Aucun fichier vidÃ©o n'a Ã©tÃ© rÃ©cupÃ©rÃ©.")

    mp4s = [p for p in files if p.lower().endswith(".mp4")]
    candidates = mp4s or files
    file_path = max(candidates, key=os.path.getsize)
    return file_path, info


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/download")
def download():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()

    if not url:
        return jsonify(error="Colle un lien TikTok."), 400
    if not is_tiktok_url(url):
        return jsonify(error="Ce lien ne semble pas venir de TikTok."), 400

    temp_dir = tempfile.mkdtemp(prefix="tiktokdl_")

    @after_this_request
    def cleanup(response):
        response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return response

    try:
        session = make_session()
        final_url, html = resolve_tiktok_url(url, session)
        post_id = post_id_from_url(final_url)

        # Detect Photo Mode before yt-dlp: yt-dlp still has incomplete support
        # for TikTok photo posts, whereas TikTok page data includes imagePost.
        item = None
        if post_id:
            item = get_tiktok_item(final_url, html, post_id, session)

        if isinstance(item, dict) and (item.get("imagePost") or {}).get("images"):
            file_path, count = render_slideshow(item, final_url, temp_dir, session)
            author = (item.get("author") or {}).get("uniqueId") or "tiktok"
            safe_author = re.sub(r"[^A-Za-z0-9._-]+", "_", str(author)).strip("_")[:40] or "tiktok"
            filename = f"{safe_author}_{post_id}_slideshow_{count}photos.mp4"
            return send_file(
                file_path,
                as_attachment=True,
                download_name=filename,
                mimetype="video/mp4",
                conditional=True,
            )

        # Normal TikTok video
        file_path, info = download_normal_video(final_url, temp_dir)
        video_id = str(info.get("id") or post_id or "video")
        uploader = str(info.get("uploader") or info.get("creator") or "tiktok")
        safe_uploader = re.sub(r"[^A-Za-z0-9._-]+", "_", uploader).strip("_")[:40] or "tiktok"
        ext = Path(file_path).suffix or ".mp4"
        filename = f"{safe_uploader}_{video_id}{ext}"

        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype="video/mp4" if ext.lower() == ".mp4" else None,
            conditional=True,
        )

    except subprocess.TimeoutExpired:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify(error="Le slideshow est trop long Ã  convertir sur le serveur gratuit."), 504
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        msg = str(exc)
        if len(msg) > 450:
            msg = msg[:450] + "â¦"
        return jsonify(error=f"TÃ©lÃ©chargement impossible. {msg}"), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
