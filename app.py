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

from curl_cffi import requests as cffi_requests

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

POST_ID_RE = re.compile(r"/(?:video|photo)/(\d+)")


def valid_tiktok_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.endswith("tiktok.com")
    except Exception:
        return False


def resolve_url(url):
    r = cffi_requests.get(
        url,
        impersonate="chrome",
        allow_redirects=True,
        timeout=20,
    )
    r.raise_for_status()
    return r.url, r.text


def post_id_from_url(url):
    m = POST_ID_RE.search(url)
    return m.group(1) if m else None


def walk_for_item(obj, post_id):
    if isinstance(obj, dict):
        if str(obj.get("id", "")) == str(post_id):
            if "imagePost" in obj or "music" in obj or "video" in obj:
                return obj

        module = obj.get("ItemModule")
        if isinstance(module, dict):
            x = module.get(str(post_id))
            if isinstance(x, dict):
                return x

        for v in obj.values():
            found = walk_for_item(v, post_id)
            if found:
                return found

    elif isinstance(obj, list):
        for v in obj:
            found = walk_for_item(v, post_id)
            if found:
                return found

    return None


def item_from_html(html, post_id):
    patterns = [
        r'<script[^>]+id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>',
        r'<script[^>]+id=["\']SIGI_STATE["\'][^>]*>(.*?)</script>',
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, html, re.I | re.S):
            try:
                data = json.loads(match.group(1))
            except Exception:
                continue

            item = walk_for_item(data, post_id)
            if item:
                return item

    return None


def ytdlp_info(url):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "impersonate": "chrome",
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def get_item(final_url, html, post_id):
    item = item_from_html(html, post_id)
    if item:
        return item

    try:
        r = cffi_requests.get(
            "https://www.tiktok.com/api/item/detail/",
            params={"itemId": post_id},
            headers={"Referer": final_url},
            impersonate="chrome",
            timeout=20,
        )

        if r.ok:
            data = r.json()
            item = (
                data.get("itemInfo", {}).get("itemStruct")
                or data.get("itemStruct")
            )

            if isinstance(item, dict):
                return item

    except Exception:
        pass

    return None


def collect_urls(value):
    out = []

    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            out.append(value)

    elif isinstance(value, list):
        for x in value:
            out.extend(collect_urls(x))

    elif isinstance(value, dict):
        for x in value.values():
            out.extend(collect_urls(x))

    return out


def unique(seq):
    seen = set()
    out = []

    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    return out


def image_url(image):
    data = image.get("imageURL") or image.get("imageUrl") or image
    urls = collect_urls(data)

    for url in urls:
        if ".heic" not in url.lower():
            return url

    return urls[0] if urls else None


def audio_urls_from_item(item):
    music = item.get("music") or {}
    urls = []

    if isinstance(music, dict):
        for key in (
            "playUrl",
            "play_url",
            "PlayUrl",
            "audioUrl",
            "audioURL",
            "downloadUrl",
            "download_url",
        ):
            urls.extend(collect_urls(music.get(key)))

        def walk(obj):
            found = []

            if isinstance(obj, dict):
                for k, v in obj.items():
                    k = str(k).lower()

                    if any(x in k for x in ("play", "audio", "download")):
                        found.extend(collect_urls(v))

                    if isinstance(v, (dict, list)):
                        found.extend(walk(v))

            elif isinstance(obj, list):
                for v in obj:
                    found.extend(walk(v))

            return found

        urls.extend(walk(music))

    return [
        u for u in unique(urls)
        if not any(
            ext in u.lower()
            for ext in (".jpg", ".jpeg", ".png", ".webp", ".heic")
        )
    ]


def audio_urls_from_ytdlp(info):
    urls = []

    if not isinstance(info, dict):
        return urls

    for f in info.get("formats") or []:
        if not isinstance(f, dict):
            continue

        url = f.get("url")
        acodec = f.get("acodec")
        vcodec = f.get("vcodec")

        if url and acodec not in (None, "none"):
            if (
                vcodec in (None, "none")
                or (
                    f.get("width") in (0, None)
                    and f.get("height") in (0, None)
                )
            ):
                urls.append(url)

    url = info.get("url")

    if isinstance(url, str):
        urls.append(url)

    return unique(urls)


def download_file(url, path, referer):
    r = cffi_requests.get(
        url,
        headers={"Referer": referer},
        impersonate="chrome",
        timeout=30,
    )
    r.raise_for_status()

    with open(path, "wb") as f:
        f.write(r.content)


def duration(path):
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

        if p.returncode == 0:
            return float(p.stdout.strip())

    except Exception:
        pass

    return 0.0


def best_audio(item, ytdlp_data, final_url, temp_dir):
    urls = unique(
        audio_urls_from_item(item)
        + audio_urls_from_ytdlp(ytdlp_data)
    )

    best_path = None
    best_duration = 0.0

    for i, url in enumerate(urls[:12]):
        path = os.path.join(temp_dir, f"audio_{i}.bin")

        try:
            download_file(url, path, final_url)

            if os.path.getsize(path) < 1000:
                continue

            d = duration(path)

            if d > best_duration:
                best_duration = d
                best_path = path

        except Exception:
            pass

    if not best_path:
        raise RuntimeError("La musique n'a pas pu être récupérée.")

    return best_path, best_duration


def create_slideshow(item, ytdlp_data, final_url, temp_dir):
    images = (item.get("imagePost") or {}).get("images") or []

    urls = [
        image_url(x)
        for x in images
        if isinstance(x, dict)
    ]

    urls = [x for x in urls if x]

    if not urls:
        raise RuntimeError(
            "TikTok n'a pas fourni les photos du slideshow."
        )

    urls = urls[:35]
    files = []

    for i, url in enumerate(urls):
        p = os.path.join(
            temp_dir,
            f"slide_{i:03d}.jpg",
        )

        download_file(
            url,
            p,
            final_url,
        )

        files.append(p)

    audio, audio_duration = best_audio(
        item,
        ytdlp_data,
        final_url,
        temp_dir,
    )

    if audio_duration < 1:
        raise RuntimeError(
            "La durée de la musique n'a pas pu être détectée."
        )

    target_duration = min(
        audio_duration,
        180.0,
    )

    per_slide = (
        target_duration
        / len(files)
    )

    concat = os.path.join(
        temp_dir,
        "slides.txt",
    )

    def esc(p):
        return (
            p
            .replace("\\", "\\\\")
            .replace("'", "'\\''")
        )

    with open(
        concat,
        "w",
        encoding="utf-8",
    ) as f:

        for p in files:
            f.write(
                f"file '{esc(p)}'\n"
            )

            f.write(
                f"duration {per_slide:.6f}\n"
            )

        f.write(
            f"file '{esc(files[-1])}'\n"
        )

    output = os.path.join(
        temp_dir,
        "slideshow.mp4",
    )

    cmd = [
        "ffmpeg",
        "-y",

        "-f",
        "concat",

        "-safe",
        "0",

        "-i",
        concat,

        "-i",
        audio,

        "-t",
        f"{target_duration:.3f}",

        "-vf",
        (
            "scale=1080:1920:"
            "force_original_aspect_ratio=decrease,"
            "pad=1080:1920:"
            "(ow-iw)/2:(oh-ih)/2,"
            "format=yuv420p"
        ),

        "-r",
        "30",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "20",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-map",
        "0:v:0",

        "-map",
        "1:a:0",

        "-movflags",
        "+faststart",

        output,
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=240,
    )

    if (
        result.returncode != 0
        or not os.path.exists(output)
    ):
        raise RuntimeError(
            "FFmpeg n'a pas réussi à créer le slideshow."
        )

    return (
        output,
        len(files),
        target_duration,
    )


def normal_video(
    url,
    temp_dir,
):
    opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(
            temp_dir,
            "%(id)s.%(ext)s",
        ),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "impersonate": "chrome",
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            url,
            download=True,
        )

    files = [
        p
        for p in glob.glob(
            os.path.join(
                temp_dir,
                "*",
            )
        )
        if os.path.isfile(p)
        and not p.endswith(".part")
        and not p.endswith(".txt")
    ]

    if not files:
        raise RuntimeError(
            "Aucune vidéo n'a été récupérée."
        )

    mp4s = [
        p
        for p in files
        if p.lower().endswith(".mp4")
    ]

    return (
        max(
            mp4s or files,
            key=os.path.getsize,
        ),
        info,
    )


@app.get("/")
def index():
    return render_template(
        "index.html"
    )


@app.get("/health")
def health():
    return {
        "ok": True
    }


@app.post("/download")
def download():
    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    url = str(
        data.get(
            "url",
            "",
        )
    ).strip()

    if not url:
        return jsonify(
            error="Colle un lien TikTok."
        ), 400

    if not valid_tiktok_url(url):
        return jsonify(
            error="Lien TikTok invalide."
        ), 400

    temp_dir = tempfile.mkdtemp(
        prefix="tiktok_"
    )

    @after_this_request
    def cleanup(response):
        response.call_on_close(
            lambda: shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )
        )

        return response

    try:
        final_url, html = resolve_url(
            url
        )

        post_id = post_id_from_url(
            final_url
        )

        ytdlp_data = ytdlp_info(
            final_url
        )

        item = None

        if post_id:
            item = get_item(
                final_url,
                html,
                post_id,
            )

        is_photo = (
            "/photo/" in final_url
        )

        if is_photo:
            if not isinstance(
                item,
                dict,
            ):
                raise RuntimeError(
                    "TikTok bloque encore "
                    "les données du slideshow "
                    "pour ce post."
                )

            output, count, d = create_slideshow(
                item,
                ytdlp_data,
                final_url,
                temp_dir,
            )

            filename = (
                f"tiktok_{post_id}_"
                f"{count}photos_"
                f"{round(d)}s.mp4"
            )

            return send_file(
                output,
                as_attachment=True,
                download_name=filename,
                mimetype="video/mp4",
            )

        video, info = normal_video(
            final_url,
            temp_dir,
        )

        filename = (
            f"tiktok_"
            f"{info.get('id', 'video')}"
            f"{Path(video).suffix or '.mp4'}"
        )

        return send_file(
            video,
            as_attachment=True,
            download_name=filename,
        )

    except Exception as e:
        return jsonify(
            error=(
                "Téléchargement impossible. "
                + str(e)[:500]
            )
        ), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "5000",
            )
        ),
    )
