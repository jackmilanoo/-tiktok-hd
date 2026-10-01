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
    if isinstance(obj, dict):
        obj_id = str(obj.get("id", ""))

        if obj_id == str(post_id) and (
            "imagePost" in obj or "video" in obj or "music" in obj
        ):
            return obj

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
    try:
        r = session.get(
            "https://www.tiktok.com/api/item/detail/",
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

    return item_from_html(html, post_id)


def urls_from_value(value):
    results = []

    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            results.append(value)

    elif isinstance(value, list):
        for x in value:
            results.extend(urls_from_value(x))

    elif isinstance(value, dict):
        for x in value.values():
            results.extend(urls_from_value(x))

    return results


def dedupe(seq):
    seen = set()
    out = []

    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    return out


def best_image_url(image):
    image_url = image.get("imageURL") or image.get("imageUrl") or image
    urls = urls_from_value(image_url)

    for u in urls:
        low = u.lower()

        if ".heic" not in low:
            return u

    return urls[0] if urls else None


def collect_music_urls(item):
    music = item.get("music") or {}
    urls = []

    if isinstance(music, dict):
        direct_keys = [
            "playUrl",
            "play_url",
            "PlayUrl",
            "audioURL",
            "audioUrl",
            "audio_url",
            "downloadUrl",
            "download_url",
        ]

        for key in direct_keys:
            if key in music:
                urls.extend(urls_from_value(music[key]))

        def walk_audio_keys(obj):
            found = []

            if isinstance(obj, dict):
                for k, v in obj.items():
                    kl = str(k).lower()

                    if any(
                        token in kl
                        for token in ("play", "audio", "download", "musicurl")
                    ):
                        found.extend(urls_from_value(v))

                    if isinstance(v, (dict, list)):
                        found.extend(walk_audio_keys(v))

            elif isinstance(obj, list):
                for v in obj:
                    found.extend(walk_audio_keys(v))

            return found

        urls.extend(walk_audio_keys(music))

    for key in (
        "musicPlayUrl",
        "music_play_url",
        "audioUrl",
        "audioURL",
    ):
        if key in item:
            urls.extend(urls_from_value(item[key]))

    filtered = []

    for u in dedupe(urls):
        low = u.lower()

        if any(
            x in low
            for x in (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".heic",
            )
        ):
            continue

        filtered.append(u)

    return filtered


def download_binary(session, url, destination, referer):
    with session.get(
        url,
        headers={
            "Referer": referer,
            "User-Agent": USER_AGENT,
        },
        stream=True,
        timeout=30,
    ) as r:

        r.raise_for_status()

        with open(destination, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


def ffprobe_duration(path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )

        if result.returncode == 0:
            return float(result.stdout.strip())

    except Exception:
        pass

    return 0.0


def choose_best_music(item, final_url, temp_dir, session):
    urls = collect_music_urls(item)

    if not urls:
        raise RuntimeError(
            "TikTok n'a fourni aucune URL audio exploitable pour ce slideshow."
        )

    best_path = None
    best_duration = 0.0

    for i, url in enumerate(urls[:8]):
        path = os.path.join(
            temp_dir,
            f"music_candidate_{i}.bin",
        )

        try:
            download_binary(
                session,
                url,
                path,
                final_url,
            )

            if (
                not os.path.exists(path)
                or os.path.getsize(path) < 1000
            ):
                continue

            duration = ffprobe_duration(path)

            if duration > best_duration:
                best_duration = duration
                best_path = path

        except Exception:
            continue

    if not best_path or best_duration < 1:
        raise RuntimeError(
            "La musique du TikTok n'a pas pu être récupérée correctement."
        )

    return best_path, best_duration


def render_slideshow(
    item,
    final_url,
    temp_dir,
    session,
):
    image_post = item.get("imagePost") or {}
    images = image_post.get("images") or []

    image_urls = [
        best_image_url(img)
        for img in images
        if isinstance(img, dict)
    ]

    image_urls = [
        u
        for u in image_urls
        if u
    ]

    if not image_urls:
        raise RuntimeError(
            "TikTok n'a pas fourni les images de ce slideshow."
        )

    image_urls = image_urls[:35]

    image_files = []

    for i, image_url in enumerate(image_urls):
        path = os.path.join(
            temp_dir,
            f"slide_{i:03d}.jpg",
        )

        download_binary(
            session,
            image_url,
            path,
            final_url,
        )

        image_files.append(path)

    audio_path, actual_audio_duration = choose_best_music(
        item,
        final_url,
        temp_dir,
        session,
    )

    meta_duration = 0.0
    music = item.get("music") or {}

    if isinstance(music, dict):
        try:
            meta_duration = float(
                music.get("duration") or 0
            )
        except Exception:
            meta_duration = 0.0

    target_duration = actual_audio_duration

    if (
        2 <= meta_duration < actual_audio_duration
    ):
        target_duration = meta_duration

    target_duration = max(
        2.0,
        min(target_duration, 180.0),
    )

    seconds_per_slide = (
        target_duration / len(image_files)
    )

    concat_path = os.path.join(
        temp_dir,
        "slides.txt",
    )

    def esc(path):
        return (
            path
            .replace("\\", "\\\\")
            .replace("'", "'\\''")
        )

    with open(
        concat_path,
        "w",
        encoding="utf-8",
    ) as f:

        for p in image_files:
            f.write(
                f"file '{esc(p)}'\n"
            )

            f.write(
                f"duration {seconds_per_slide:.6f}\n"
            )

        f.write(
            f"file '{esc(image_files[-1])}'\n"
        )

    output = os.path.join(
        temp_dir,
        "slideshow.mp4",
    )

    vf = (
        "scale=1080:1920:"
        "force_original_aspect_ratio=decrease,"
        "pad=1080:1920:"
        "(ow-iw)/2:(oh-ih)/2,"
        "format=yuv420p"
    )

    cmd = [
        "ffmpeg",
        "-y",

        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_path,

        "-i",
        audio_path,

        "-t",
        f"{target_duration:.3f}",

        "-vf",
        vf,

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
        err = (
            result.stderr or ""
        )[-900:]

        raise RuntimeError(
            "FFmpeg n'a pas pu créer "
            f"le slideshow avec musique. {err}"
        )

    final_duration = ffprobe_duration(output)

    if final_duration < min(
        target_duration * 0.85,
        target_duration - 1,
    ):
        raise RuntimeError(
            "La vidéo créée est trop courte "
            f"({final_duration:.1f}s au lieu "
            f"d'environ {target_duration:.1f}s)."
        )

    return (
        output,
        len(image_files),
        target_duration,
    )


def download_normal_video(
    url,
    temp_dir,
):
    outtmpl = os.path.join(
        temp_dir,
        "%(id)s.%(ext)s",
    )

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
            "Aucun fichier vidéo n'a été récupéré."
        )

    mp4s = [
        p
        for p in files
        if p.lower().endswith(".mp4")
    ]

    candidates = (
        mp4s or files
    )

    file_path = max(
        candidates,
        key=os.path.getsize,
    )

    return (
        file_path,
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
    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    url = str(
        payload.get(
            "url",
            "",
        )
    ).strip()

    if not url:
        return jsonify(
            error="Colle un lien TikTok."
        ), 400

    if not is_tiktok_url(url):
        return jsonify(
            error="Ce lien ne semble pas venir de TikTok."
        ), 400

    temp_dir = tempfile.mkdtemp(
        prefix="tiktokdl_"
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
        session = make_session()

        final_url, html = resolve_tiktok_url(
            url,
            session,
        )

        post_id = post_id_from_url(
            final_url
        )

        is_photo_url = (
            "/photo/" in final_url
        )

        item = None

        if post_id:
            item = get_tiktok_item(
                final_url,
                html,
                post_id,
                session,
            )

        if is_photo_url:
            if not isinstance(
                item,
                dict,
            ):
                raise RuntimeError(
                    "Le post photo est détecté, "
                    "mais TikTok n'a pas fourni "
                    "les données nécessaires."
                )

            if not (
                item.get("imagePost")
                or {}
            ).get("images"):
                raise RuntimeError(
                    "TikTok n'a pas fourni "
                    "la liste complète des photos."
                )

            (
                file_path,
                count,
                duration,
            ) = render_slideshow(
                item,
                final_url,
                temp_dir,
                session,
            )

            author = (
                (
                    item.get("author")
                    or {}
                ).get("uniqueId")
                or "tiktok"
            )

            safe_author = (
                re.sub(
                    r"[^A-Za-z0-9._-]+",
                    "_",
                    str(author),
                )
                .strip("_")[:40]
                or "tiktok"
            )

            filename = (
                f"{safe_author}_"
                f"{post_id}_"
                f"slideshow_"
                f"{count}photos_"
                f"{round(duration)}s.mp4"
            )

            return send_file(
                file_path,
                as_attachment=True,
                download_name=filename,
                mimetype="video/mp4",
                conditional=True,
            )

        if (
            isinstance(
                item,
                dict,
            )
            and (
                item.get("imagePost")
                or {}
            ).get("images")
        ):
            (
                file_path,
                count,
                duration,
            ) = render_slideshow(
                item,
                final_url,
                temp_dir,
                session,
            )

            author = (
                (
                    item.get("author")
                    or {}
                ).get("uniqueId")
                or "tiktok"
            )

            safe_author = (
                re.sub(
                    r"[^A-Za-z0-9._-]+",
                    "_",
                    str(author),
                )
                .strip("_")[:40]
                or "tiktok"
            )

            filename = (
                f"{safe_author}_"
                f"{post_id or 'photo'}_"
                f"slideshow_"
                f"{count}photos_"
                f"{round(duration)}s.mp4"
            )

            return send_file(
                file_path,
                as_attachment=True,
                download_name=filename,
                mimetype="video/mp4",
                conditional=True,
            )

        file_path, info = download_normal_video(
            final_url,
            temp_dir,
        )

        video_id = str(
            info.get("id")
            or post_id
            or "video"
        )

        uploader = str(
            info.get("uploader")
            or info.get("creator")
            or "tiktok"
        )

        safe_uploader = (
            re.sub(
                r"[^A-Za-z0-9._-]+",
                "_",
                uploader,
            )
            .strip("_")[:40]
            or "tiktok"
        )

        ext = (
            Path(file_path).suffix
            or ".mp4"
        )

        filename = (
            f"{safe_uploader}_"
            f"{video_id}"
            f"{ext}"
        )

        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype=(
                "video/mp4"
                if ext.lower() == ".mp4"
                else None
            ),
            conditional=True,
        )

    except subprocess.TimeoutExpired:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

        return jsonify(
            error=(
                "Le slideshow est trop long "
                "à convertir sur le serveur gratuit."
            )
        ), 504

    except Exception as exc:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )

        msg = str(exc)

        if len(msg) > 500:
            msg = (
                msg[:500]
                + "…"
            )

        return jsonify(
            error=(
                "Téléchargement impossible. "
                + msg
            )
        ), 500


if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            "5000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
