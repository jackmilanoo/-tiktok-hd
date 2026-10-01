
from flask import Flask, render_template, request, send_file, jsonify, after_this_request
import yt_dlp
import tempfile
import shutil
import os
import glob
import re
from pathlib import Path

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

TIKTOK_HOST_RE = re.compile(r"(^|\.)tiktok\.com$", re.I)

def is_tiktok_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return bool(TIKTOK_HOST_RE.search(host))
    except Exception:
        return False

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
    outtmpl = os.path.join(temp_dir, "%(id)s.%(ext)s")

    @after_this_request
    def cleanup(response):
        # send_file may still be streaming, so cleanup is handled by
        # call_on_close rather than deleting immediately.
        response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return response

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

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        files = [
            p for p in glob.glob(os.path.join(temp_dir, "*"))
            if os.path.isfile(p) and not p.endswith(".part")
        ]
        if not files:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify(error="Aucun fichier vidéo n'a été récupéré."), 500

        mp4s = [p for p in files if p.lower().endswith(".mp4")]
        candidates = mp4s or files
        file_path = max(candidates, key=os.path.getsize)

        video_id = str(info.get("id") or "video")
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

    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        msg = str(exc)
        # Avoid dumping a huge backend trace-like message into the UI.
        if len(msg) > 300:
            msg = msg[:300] + "…"
        return jsonify(error=f"Téléchargement impossible. {msg}"), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
