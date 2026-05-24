from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time
import re
from pathlib import Path

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

COOKIES_FILE = "cookies.txt"

jobs = {}

def clean_filename(name):
    return re.sub(r'[^\w\s\-_.]', '', name)[:80]

def get_base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts

def run_download(job_id, url, quality, fmt):
    jobs[job_id]["status"] = "downloading"

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                jobs[job_id]["progress"] = round(downloaded / total * 100, 1)
                jobs[job_id]["speed"] = d.get("_speed_str", "").strip()
                jobs[job_id]["eta"] = d.get("_eta_str", "").strip()
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 99

    output_path = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    opts = get_base_opts()
    opts["outtmpl"] = output_path
    opts["progress_hooks"] = [progress_hook]

    if fmt == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        height_map = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360, "240p": 240}
        height = height_map.get(quality, 720)
        opts["format"] = (
    f"bestvideo[height<={height}]+bestaudio/"
    f"best[height<={height}]/"
    f"best"
        )
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = clean_filename(info.get("title", "video"))
            ext = "mp3" if fmt == "mp3" else "mp4"
            jobs[job_id]["title"] = title
            jobs[job_id]["filename"] = f"{title}.{ext}"
            for f in DOWNLOAD_DIR.iterdir():
                if f.stem == job_id:
                    jobs[job_id]["filepath"] = str(f)
                    break
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100

        def cleanup():
            time.sleep(600)
            fp = jobs[job_id].get("filepath")
            if fp and os.path.exists(fp):
                os.remove(fp)
            jobs.pop(job_id, None)
        threading.Thread(target=cleanup, daemon=True).start()

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        opts = get_base_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        seen = set()
        for f in (info.get("formats") or []):
            h = f.get("height")
            if h and h not in seen:
                seen.add(h)
                size = f.get("filesize") or f.get("filesize_approx")
                formats.append({
                    "quality": f"{h}p",
                    "size_mb": round(size / 1_000_000, 1) if size else None
                })
        formats.sort(key=lambda x: int(x["quality"][:-1]), reverse=True)
        if not formats:
            formats = [
                {"quality": "1080p", "size_mb": None},
                {"quality": "720p", "size_mb": None},
                {"quality": "480p", "size_mb": None},
                {"quality": "360p", "size_mb": None},
            ]

        return jsonify({
            "title": info.get("title", "Video"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration_string") or "",
            "uploader": info.get("uploader") or info.get("channel", ""),
            "view_count": info.get("view_count"),
            "platform": info.get("extractor_key", ""),
            "formats": formats[:6],
        })
    except Exception as e:
        err = str(e)
        if "Sign in" in err or "bot" in err.lower():
            err = "YouTube is blocking this request. Try again or use a different video."
        elif "Private" in err or "private" in err:
            err = "This video is private."
        elif "unavailable" in err.lower():
            err = "This video is unavailable."
        return jsonify({"error": err}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json or {}
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p")
    fmt = data.get("format", "mp4").lower()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "speed": "",
        "eta": "",
        "filename": "",
        "filepath": None,
        "error": None,
    }
    t = threading.Thread(target=run_download, args=(job_id, url, quality, fmt), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    fp = job.get("filepath")
    if not fp or not os.path.exists(fp):
        return jsonify({"error": "File not found"}), 404
    return send_file(fp, as_attachment=True, download_name=job["filename"])


@app.route("/api/ping")
def ping():
    cookies_loaded = os.path.exists(COOKIES_FILE)
    return jsonify({"status": "ok", "cookies": cookies_loaded})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
