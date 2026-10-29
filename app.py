
import os
import uuid
import hmac
import subprocess
from pathlib import Path
from flask import (
    Flask, request, session, redirect,
    render_template_string, send_file, abort
)

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=200 * 1024 * 1024
)

PASSWORD = os.environ.get("STUDIO_PASSWORD", "")
BASE = Path("/tmp/ai-short-studio")
BASE.mkdir(parents=True, exist_ok=True)

HTML = """
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>AI Short Studio</title>
<style>
body {
 background:#11131c;
 color:white;
 font-family:Arial,sans-serif;
 max-width:480px;
 margin:30px auto;
 padding:20px;
}
h1 {color:#a99aff}
.card {
 background:#232637;
 padding:22px;
 border-radius:18px;
}
input,button {
 box-sizing:border-box;
 width:100%;
 padding:14px;
 margin:10px 0;
 border-radius:9px;
}
button {
 background:#806bff;
 border:0;
 color:white;
 font-weight:bold;
}
input {background:white;color:black}
video {width:100%;border-radius:12px}
a {color:#c1b8ff}
.error {color:#ff8d8d}
</style>
</head>
<body>
<h1>AI Short Studio</h1>
<div class="card">
{% if login %}
<h3>ログイン</h3>
{% if error %}
<p class="error">{{error}}</p>
{% endif %}
<form action="/login" method="post">
<input type="password" name="password"
placeholder="パスワード" required>
<button type="submit">ログイン</button>
</form>
{% else %}
<h3>動画をアップロード</h3>
<form action="/edit" method="post"
enctype="multipart/form-data">
<input type="file" name="video"
accept=".mp4,.mov" required>
<p>編集スタイル：テンポ重視</p>
<button type="submit">編集スタート</button>
</form>

{% if video %}
<h3>完成動画</h3>
<video controls src="/video/{{video}}"></video>
{% if approved %}
<p>承認済み</p>
{% else %}
<form action="/approve/{{video}}" method="post">
<button type="submit">動画を承認</button>
</form>
{% endif %}
<a href="/download/{{video}}">MP4を保存</a>
{% endif %}
<a href="/logout">ログアウト</a>
{% endif %}
</div>
</body>
</html>
"""

def authorized():
    return (
        bool(PASSWORD)
        and bool(app.secret_key)
        and session.get("ok") is True
    )

@app.route("/")
def home():
    return render_template_string(
        HTML,
        login=not authorized(),
        error=request.args.get("error"),
        video=None,
        approved=False
    )

@app.route("/login", methods=["POST"])
def login():
    if not PASSWORD:
        return redirect("/?error=STUDIO_PASSWORD未設定")

    if not app.secret_key:
        return redirect("/?error=SECRET_KEY未設定")

    entered = request.form.get("password", "")

    if not hmac.compare_digest(entered, PASSWORD):
        return redirect("/?error=パスワードが違います")

    session.clear()
    session["ok"] = True
    return redirect("/")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/edit", methods=["POST"])
def edit():
    if not authorized():
        abort(401)

    file = request.files.get("video")
    if (
        not file
        or not file.filename
        or not file.filename.lower().endswith(
            (".mp4", ".mov")
        )
    ):
        abort(400, "MP4またはMOVを選択してください")

    video_id = uuid.uuid4().hex
    source = BASE / (video_id + ".input")
    output = BASE / (video_id + ".mp4")
    file.save(source)

    command = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-t", "60",
        "-vf",
        "select='lt(mod(t,3),1.6)',"
        "setpts=N/FRAME_RATE/TB,"
        "scale=1080:1920:"
        "force_original_aspect_ratio=increase,"
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
        subprocess.run(
            command,
            check=True,
            timeout=180,
            capture_output=True
        )
    except subprocess.TimeoutExpired:
        abort(500, "動画処理が時間切れになりました")
    except subprocess.CalledProcessError:
        abort(500, "動画の変換に失敗しました")
    finally:
        source.unlink(missing_ok=True)

    return render_template_string(
        HTML,
        login=False,
        error=None,
        video=video_id,
        approved=False
    )

def output_file(video_id):
    try:
        uuid.UUID(hex=video_id)
    except (ValueError, AttributeError):
        abort(404)

    path = BASE / (video_id + ".mp4")

    if not path.is_file():
        abort(404)

    return path

@app.route("/video/<video_id>")
def video(video_id):
    if not authorized():
        abort(401)
    return send_file(output_file(video_id))

@app.route("/approve/<video_id>", methods=["POST"])
def approve(video_id):
    if not authorized():
        abort(401)

    output_file(video_id)

    return render_template_string(
        HTML,
        login=False,
        error=None,
        video=video_id,
        approved=True
    )

@app.route("/download/<video_id>")
def download(video_id):
    if not authorized():
        abort(401)

    return send_file(
        output_file(video_id),
        as_attachment=True,
        download_name="ai-short-studio.mp4"
    )

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000"))
    )
