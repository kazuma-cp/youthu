
import os
import uuid
import subprocess
from pathlib import Path
from flask import Flask, request, render_template_string, send_file, abort, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
PASSWORD = os.environ.get("STUDIO_PASSWORD", "")
BASE = Path("/tmp/ai-short-studio")
BASE.mkdir(exist_ok=True)

HTML = """
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{background:#10121b;color:white;font-family:Arial;
max-width:480px;margin:35px auto;padding:18px}
h1{color:#9c8cff}
.card{background:#202433;padding:22px;border-radius:18px}
button,input{padding:13px;margin:10px 0;max-width:100%}
button{background:#806bff;color:white;border:0;border-radius:10px}
video{width:100%;border-radius:12px}
a{color:#bcb0ff}
</style>
<h1>AI Short Studio</h1>
<div class="card">
{% if login %}
<h3>Login</h3>
<form method="post" action="/login">
<input type="password" name="password" placeholder="Password">
<button>Login</button>
</form>
{% else %}
<h3>Upload your video</h3>
<form action="/edit" method="post" enctype="multipart/form-data">
<input type="file" name="video" accept="video/mp4,video/quicktime" required>
<p>Editing style: Fast-paced</p>
<button>Start Editing</button>
</form>
{% if video %}
<h3>Preview</h3>
<video controls src="/video/{{video}}"></video>
<p>
{% if approved %}
Approved for publishing
{% else %}
<form action="/approve/{{video}}" method="post">
<button>Approve Video</button>
</form>
{% endif %}
</p>
<a href="/download/{{video}}">Download MP4</a>
{% endif %}
{% endif %}
</div>
"""

def authorized():
    return bool(PASSWORD) and session.get("ok") is True

@app.route("/")
def home():
    return render_template_string(
        HTML, login=not authorized(),
        video=None, approved=False
    )

@app.route("/login", methods=["POST"])
def login():
    if PASSWORD and request.form.get("password") == PASSWORD:
        session["ok"] = True
    return redirect("/")

@app.route("/edit", methods=["POST"])
def edit():
    if not authorized():
        abort(401)

    file = request.files.get("video")
    if not file or not file.filename.lower().endswith((".mp4", ".mov")):
        abort(400)

    video_id = uuid.uuid4().hex
    source = BASE / (video_id + ".input")
    output = BASE / (video_id + ".mp4")

    file.save(source)

    command = [
        "ffmpeg", "-y", "-i", str(source),
        "-t", "60",
        "-vf",
        "select='lt(mod(t,3),1.6)',"
        "setpts=N/FRAME_RATE/TB,"
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-movflags", "+faststart",
        str(output)
    ]

    try:
        subprocess.run(command, check=True, timeout=180)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        abort(500, "Video processing failed")
    finally:
        source.unlink(missing_ok=True)

    return render_template_string(
        HTML, login=False, video=video_id, approved=False
    )

@app.route("/video/<video_id>")
def video(video_id):
    if not authorized():
        abort(401)
    if not video_id.isalnum():
        abort(400)
    return send_file(BASE / (video_id + ".mp4"))

@app.route("/approve/<video_id>", methods=["POST"])
def approve(video_id):
    if not authorized():
        abort(401)
    if not video_id.isalnum():
        abort(400)
    if not (BASE / (video_id + ".mp4")).exists():
        abort(404)
    return render_template_string(
        HTML, login=False, video=video_id, approved=True
    )

@app.route("/download/<video_id>")
def download(video_id):
    if not authorized():
        abort(401)
    if not video_id.isalnum():
        abort(400)
    return send_file(
        BASE / (video_id + ".mp4"),
        as_attachment=True
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
