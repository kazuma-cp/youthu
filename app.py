"""AI Short Studio - low-cost, permission-based Shorts MVP.

Requires ffmpeg with libass, yt-dlp, and faster-whisper; CPU inference may be slow.
"""
import hmac
import textwrap
import math
import os
import re
import shutil
import subprocess
import uuid
import wave
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template_string, request, send_file, session

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', '')
app.config.update(MAX_CONTENT_LENGTH=400 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='Lax')
PASSWORD = os.getenv('STUDIO_PASSWORD', '')
ROOT = Path('/tmp/ai-short-studio')
ROOT.mkdir(parents=True, exist_ok=True)
MODEL_NAME = os.getenv('WHISPER_MODEL', 'tiny')  # tiny is practical on small CPU machines
MAX_SOURCE_SECONDS = int(os.getenv('MAX_SOURCE_SECONDS', '3600'))
MAX_JOBS_BYTES = 650 * 1024 * 1024

HTML = '''<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AI Short Studio</title>
<style>body{background:#11131c;color:white;font:16px system-ui;max-width:560px;margin:25px auto;padding:18px}
.card{background:#242736;padding:23px;border-radius:16px}h1{color:#ac9cff}label{display:block;margin-top:14px}
input,button,select{box-sizing:border-box;width:100%;padding:13px;margin-top:7px;border:0;border-radius:9px}
button{background:#7769f4;color:#fff;font-weight:bold}input[type=checkbox]{width:auto}video{width:100%;max-height:75vh}
a{color:#b9adff}.error{color:#ffa0a0}.muted{color:#c0c3d4;font-size:13px}</style></head><body>
<h1>AI Short Studio</h1><div class="card">
{% if login %}<h3>ログイン</h3><form method="post" action="/login"><input type="password" name="password" required placeholder="パスワード"><button>ログイン</button></form>
{% else %}<h3>見出し付き・元字幕を活かすShorts</h3>
<form method="post" action="/edit" enctype="multipart/form-data">
<label>動画ファイル（MP4・MOV）</label><input type="file" name="video" accept=".mp4,.mov">
<label>または YouTube URL（利用権限がある動画のみ）</label><input name="url" type="url" placeholder="https://www.youtube.com/watch?v=...">
<label>上の赤い見出し（元動画の内容と一致する言葉）</label><input name="headline" maxlength="30" placeholder="例：ヒカルが語る意外な助言" required>
<label>字幕設定</label><select name="caption_mode"><option value="original">元字幕をそのまま残す（追加字幕なし）</option><option value="auto">元字幕がない素材のみ自動字幕を追加</option></select>
<label>長さ</label><select name="length"><option value="35">35秒</option><option value="30">30秒</option><option value="40">40秒</option><option value="45">45秒</option></select>
<label>開始位置（秒） — 会話の導入から指定推奨。空欄は音量による暫定選定</label><input type="number" min="0" name="start" step="1" placeholder="自動で選ぶ">
<label><input type="checkbox" name="licensed" required> この素材を編集・利用する権利を持っています</label>
<button type="submit">赤い見出し＋元音声で生成</button>
</form><p class="muted">元字幕は映像の一部として保持します。会話の途中切れを防ぐには、導入から結論までを含む開始秒数を指定してください。空欄の場合は音量を使った仮選定で、文脈確認はできません。</p>
{% if video %}<h3>生成結果</h3><p>{{info}}</p><video controls preload="metadata" src="/video/{{video}}"></video>
<p><a href="/download/{{video}}">MP4を保存</a></p><p>公開前に音声・字幕・権利を必ず確認してください。</p>{% endif %}
<a href="/logout">ログアウト</a>{% endif %}
{% if error %}<p class="error">{{error}}</p>{% endif %}</div></body></html>'''


def authorized():
    return bool(PASSWORD and app.secret_key and session.get('ok') is True)


def run(command, timeout=300):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def media_duration(path):
    result = run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                  '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], 30)
    duration = float(result.stdout.strip())
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_SOURCE_SECONDS:
        raise ValueError('素材は1時間以内の音声付き動画を使用してください')
    return duration


def automatic_start(source, duration, length):
    """Pick a relatively active contiguous segment, using per-second audio RMS."""
    max_start = max(0, int(duration - length))
    if max_start == 0:
        return 0
    audio = source.with_suffix('.wav')
    try:
        run(['ffmpeg', '-nostdin', '-y', '-i', str(source), '-vn', '-ac', '1',
             '-ar', '8000', '-t', str(int(duration)), '-c:a', 'pcm_s16le', str(audio)], 120)
        with wave.open(str(audio), 'rb') as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                return 0
            sample_rate = w.getframerate()
            # Rough per-second RMS. stdlib audioop was removed in Python 3.13.
            import array
            rms = []
            while True:
                block = w.readframes(sample_rate)
                if not block:
                    break
                a = array.array('h')
                a.frombytes(block)
                if not a:
                    break
                rms.append(math.sqrt(sum(float(v) * v for v in a) / len(a)))
        if len(rms) < length:
            return 0
        # Prefer active speech while penalizing dead air and over-loud spikes.
        scores = [min(math.log1p(x), 9.0) if x > 130 else 0 for x in rms]
        rolling = sum(scores[:length])
        best_score, best = rolling, 0
        for i in range(1, min(max_start, len(scores)-length)+1):
            rolling += scores[i+length-1] - scores[i-1]
            if rolling > best_score:
                best_score, best = rolling, i
        return best
    except (subprocess.SubprocessError, OSError, ValueError):
        return 0
    finally:
        audio.unlink(missing_ok=True)


def download_authorized_youtube(url, target):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or (parsed.hostname or '').lower() not in {
        'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}:
        raise ValueError('YouTubeのHTTPS URLのみ入力できます')
    # yt-dlp downloads are not guaranteed: YouTube may block the request.
    import yt_dlp
    options = {'format': 'best[ext=mp4]/best', 'outtmpl': str(target.with_suffix('.%(ext)s')),
               'noplaylist': True, 'quiet': True, 'no_warnings': True,
               'max_filesize': 400 * 1024 * 1024,
               'socket_timeout': 15, 'retries': 1,
               'match_filter': yt_dlp.utils.match_filter_func(f'duration < {MAX_SOURCE_SECONDS + 1}')}
    with yt_dlp.YoutubeDL(options) as dl:
        dl.download([url])
    candidates = [p for p in target.parent.glob(target.stem + '.*') if p.is_file()]
    if len(candidates) != 1:
        raise ValueError('動画の取得に失敗しました。MP4アップロードを試してください')
    candidates[0].rename(target)


def subtitles_srt(audio_file, output):
    from faster_whisper import WhisperModel
    model = WhisperModel(MODEL_NAME, device='cpu', compute_type='int8', cpu_threads=2)
    segments, _ = model.transcribe(str(audio_file), language='ja', beam_size=1,
                                    vad_filter=True, condition_on_previous_text=False)
    def stamp(sec):
        ms = max(0, round(sec * 1000))
        return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}'
    count = 0
    with output.open('w', encoding='utf-8') as stream:
        for seg in segments:
            content = re.sub(r'\s+', ' ', seg.text).strip()
            if not content or seg.end <= seg.start:
                continue
            # Chunk long transcriptions to avoid very wide caption lines.
            chunks = [content[i:i+18] for i in range(0, len(content), 18)]
            for idx, chunk in enumerate(chunks):
                start = seg.start + (seg.end - seg.start) * idx / len(chunks)
                end = seg.start + (seg.end - seg.start) * (idx+1) / len(chunks)
                count += 1
                stream.write(f'{count}\n{stamp(start)} --> {stamp(end)}\n{chunk}\n\n')
    return count


def create_clip(source, folder, length, start, headline, caption_mode='original'):
    """Keep uninterrupted original dialogue/audio and original burned-in captions.

    Place a separate red headline OUTSIDE the source image area so it does not
    cover pre-existing captions or people's faces. Never splice speakers.
    """
    duration = media_duration(source)
    length = min(length, int(duration))
    if length < 3:
        raise ValueError('動画が短すぎます')
    if start is None:
        start = automatic_start(source, duration, length)
    else:
        start = min(start, max(0, int(duration)-length))
    headline = re.sub(r'\s+', ' ', headline).strip()
    if not headline or len(headline) > 30 or any(ord(c) < 32 for c in headline):
        raise ValueError('見出しは1〜30文字にしてください')
    # Break on Japanese character boundaries; 13 characters at 44 px fits 720px.
    parts = textwrap.wrap(headline, width=13, break_long_words=True, break_on_hyphens=False)
    if len(parts) > 2:
        raise ValueError('見出しを短くしてください（最大2行）')
    header_file = folder / 'headline.txt'
    header_file.write_text('\n'.join(parts), encoding='utf-8')
    font = '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'
    if not Path(font).is_file():
        raise ValueError('日本語フォントがありません')
    output = folder / 'result.mp4'
    # FIT rather than crop: preserve the entire original frame, including both
    # interview speakers and any existing captions. Letterboxing is intentional.
    # The top 160px is a red headline band; original audio stays continuous.
    vf = ("scale=720:1120:force_original_aspect_ratio=decrease,"
          "pad=720:1120:(ow-iw)/2:(oh-ih)/2:black,"
          "pad=720:1280:0:160:black,"
          "drawbox=x=0:y=0:w=iw:h=160:color=0xE40000:t=fill,"
          f"drawtext=fontfile={font}:textfile={header_file}:"
          "fontcolor=white:fontsize=44:line_spacing=3:"
          "x=(w-text_w)/2:y=(160-text_h)/2")
    count = 0
    if caption_mode == 'auto':
        audio = folder / 'clip.wav'
        subs = folder / 'captions.srt'
        run(['ffmpeg', '-nostdin', '-y', '-ss', str(start), '-i', str(source),
             '-t', str(length), '-vn', '-ac', '1', '-ar', '16000',
             '-c:a', 'pcm_s16le', str(audio)], 120)
        count = subtitles_srt(audio, subs)
        if count:
            # Subtitle position within preserved original-image area.
            vf += (",subtitles={subs}:force_style='FontName=Noto Sans CJK JP,"
                   "FontSize=17,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                   "BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=115'")
    run(['ffmpeg', '-nostdin', '-y', '-ss', str(start), '-i', str(source),
         '-t', str(length), '-vf', vf, '-map', '0:v:0', '-map', '0:a:0',
         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '24',
         '-pix_fmt', 'yuv420p', '-r', '30', '-c:a', 'aac', '-b:a', '128k',
         '-movflags', '+faststart', str(output)], 600)
    return start, length, count


@app.get('/')
def home():
    return render_template_string(HTML, login=not authorized(), video=None, error=None, info=None)

@app.post('/login')
def login():
    if not PASSWORD or not app.secret_key:
        return render_template_string(HTML, login=True, video=None, error='管理者に環境変数の設定を依頼してください', info=None), 503
    if not hmac.compare_digest(request.form.get('password', ''), PASSWORD):
        return render_template_string(HTML, login=True, video=None, error='パスワードが違います', info=None), 403
    session.clear()
    session['ok'] = True
    return redirect('/')

@app.get('/logout')
def logout():
    session.clear()
    return redirect('/')


@app.post('/edit')
def edit():
    if not authorized():
        abort(401)
    if request.form.get('licensed') != 'on':
        abort(400, '素材の利用権限を確認してください')
    upload = request.files.get('video')
    url = request.form.get('url', '').strip()
    if bool(upload and upload.filename) == bool(url):
        abort(400, '動画ファイルかYouTube URLのどちらか一方を指定してください')
    folder = ROOT / uuid.uuid4().hex
    folder.mkdir(mode=0o700)
    source = folder / 'source.mp4'
    try:
        if upload and upload.filename:
            if Path(upload.filename).suffix.lower() not in ('.mp4', '.mov'):
                raise ValueError('MP4またはMOVのみ利用できます')
            upload.save(source)
        else:
            download_authorized_youtube(url, source)
        if source.stat().st_size > MAX_JOBS_BYTES:
            raise ValueError('動画ファイルが大きすぎます')
        length = int(request.form.get('length', '35'))
        if length not in (30, 35, 40, 45):
            raise ValueError('動画の長さが不正です')
        start_value = request.form.get('start', '').strip()
        start = int(start_value) if start_value else None
        if start is not None and start < 0:
            raise ValueError('開始秒数は0以上にしてください')
        mode = request.form.get('caption_mode', 'original')
        if mode not in ('original', 'auto'):
            raise ValueError('字幕設定が不正です')
        pos, seconds, caption_count = create_clip(
            source, folder, length, start, request.form.get('headline', ''), mode)
        info = f'元動画の{pos}秒から{seconds}秒・元音声を保持・赤い見出しあり・追加字幕{caption_count}件'
        return render_template_string(HTML, login=False, video=folder.name, error=None, info=info)
    except (ValueError, OSError, subprocess.SubprocessError, Exception) as exc:
        # Never surface raw yt-dlp errors or URLs that could include private information.
        app.logger.warning('Failed to create clip: %s', str(exc)[:500])
        shutil.rmtree(folder, ignore_errors=True)
        return render_template_string(HTML, login=False, video=None,
            error='動画生成に失敗しました。動画形式、字幕用の音声、YouTubeの取得制限、サーバーの空き容量を確認してください。', info=None), 422
    finally:
        source.unlink(missing_ok=True)
        (folder / 'clip.wav').unlink(missing_ok=True)


def clip_path(video_id):
    if not re.fullmatch(r'[0-9a-f]{32}', video_id):
        abort(404)
    path = ROOT / video_id / 'result.mp4'
    if not path.is_file():
        abort(404)
    return path


@app.get('/video/<video_id>')
def video(video_id):
    if not authorized():
        abort(401)
    return send_file(clip_path(video_id), mimetype='video/mp4')


@app.get('/download/<video_id>')
def download(video_id):
    if not authorized():
        abort(401)
    return send_file(clip_path(video_id), as_attachment=True,
                     download_name='ai-short-studio.mp4')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
