"""AI Short Studio - low-cost, permission-based Shorts MVP.

Requires ffmpeg with libass, yt-dlp, and faster-whisper; CPU inference may be slow.
"""
import hmac
import textwrap
import math
import statistics
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

HTML = '''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AI Short Studio</title>
<style>body{background:#11131c;color:white;font:16px system-ui;max-width:680px;margin:20px auto;padding:16px}.card{background:#242736;padding:20px;border-radius:16px}h1{color:#ac9cff}label{display:block;margin-top:12px}input,button,select{box-sizing:border-box;width:100%;padding:12px;margin-top:7px;border:0;border-radius:9px}button{background:#7769f4;color:#fff;font-weight:bold}input[type=checkbox]{width:auto}video{width:100%;max-height:70vh}a{color:#b9adff}.error{color:#ffa0a0}.muted{color:#c0c3d4;font-size:13px}.clip{border:1px solid #565b70;border-radius:12px;padding:12px;margin:12px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.clip h4{margin:0 0 5px}</style></head><body>
<h1>AI Short Studio</h1><div class="card">
{% if login %}<h3>ログイン</h3><form method="post" action="/login"><input type="password" name="password" required placeholder="パスワード"><button>ログイン</button></form>
{% else %}<h3>MP4を1回アップロード → AIが見どころを自動選定</h3>
<form method="post" action="/batch" enctype="multipart/form-data">
<label>元動画ファイル（MP4・MOV、最大400MB）</label><input type="file" name="video" accept=".mp4,.mov" required>
<p class="muted">YouTube URLの直接取得はサーバー側でブロックされる場合があるため、権利のある動画ファイルを使用してください。</p>
<label>字幕設定</label><select name="caption_mode"><option value="original">元字幕をそのまま残す（追加字幕なし）</option><option value="auto">元字幕がない素材のみ自動字幕を追加</option></select>
<label>生成本数</label><select name="count"><option value="3">3本</option><option value="4" selected>4本</option><option value="5">5本</option></select>
<label><input type="checkbox" name="manual"> 手動で見出し・開始位置を指定する（通常は不要）</label><p class="muted">通常は入力不要。音声の文字起こしと発話密度から、重複しない場面を自動選定します。選定結果は必ず確認してください。</p>
{% for i in range(1,6) %}<div class="clip"><h4>切り抜き {{i}}</h4><label>赤い見出し</label><input name="headline_{{i}}" maxlength="26" placeholder="例：堀大輔の発言に細川が反論"><div class="grid"><label>開始位置（秒）<input name="start_{{i}}" type="number" min="0" step="1" placeholder="1020"></label><label>長さ<select name="length_{{i}}"><option value="35">35秒</option><option value="30">30秒</option><option value="40">40秒</option><option value="45">45秒</option></select></label></div></div>{% endfor %}
<label><input type="checkbox" name="licensed" required> この素材を編集・利用する権利を持っています</label>
<button type="submit">見どころを自動分析して3〜5本生成</button></form>
<p class="muted">生成中は画面を閉じないでください。長尺動画の文字起こしには時間がかかり、Renderのタイムアウトで失敗する可能性があります。見出しは発言の一部を引用した暫定案です。公開前に文脈・正確性・権利を確認してください。</p>
{% if results %}<h3>生成結果：{{results|length}}本</h3>{% for item in results %}<div class="clip"><h4>{{item.headline}}</h4><p>{{item.start}}秒から{{item.length}}秒</p><video controls preload="metadata" src="/video/{{item.id}}"></video><p><a href="/download/{{item.id}}">このMP4を保存</a></p></div>{% endfor %}{% endif %}
{% if warnings %}{% for warning in warnings %}<p class="error">{{warning}}</p>{% endfor %}{% endif %}
<a href="/logout">ログアウト</a>{% endif %}
{% if error %}<p class="error">{{error}}</p>{% endif %}</div></body></html>'''


def page(**kwargs):
    values = dict(login=not authorized(), results=None, warnings=None, error=None)
    values.update(kwargs)
    return render_template_string(HTML, **values)



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
            vf += (f",subtitles={subs}:force_style='FontName=Noto Sans CJK JP,"
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
    return page()

@app.post('/login')
def login():
    if not PASSWORD or not app.secret_key:
        return page(login=True, error='管理者に環境変数の設定を依頼してください'), 503
    if not hmac.compare_digest(request.form.get('password', ''), PASSWORD):
        return page(login=True, error='パスワードが違います'), 403
    session.clear()
    session['ok'] = True
    return redirect('/')

@app.get('/logout')
def logout():
    session.clear()
    return redirect('/')

def select_highlights(source, duration, count):
    """Transcript-grounded heuristic ranking; no virality or semantic guarantee."""
    from faster_whisper import WhisperModel
    audio = source.parent / 'analysis.wav'
    run(['ffmpeg', '-nostdin', '-y', '-i', str(source), '-vn', '-ac', '1',
         '-ar', '16000', '-c:a', 'pcm_s16le', str(audio)], 240)
    try:
        model = WhisperModel(MODEL_NAME, device='cpu', compute_type='int8', cpu_threads=2)
        segments, _ = model.transcribe(str(audio), language='ja', beam_size=1,
                                      vad_filter=True, condition_on_previous_text=False)
        transcript = [(float(x.start), float(x.end), re.sub(r'\s+', '', x.text))
                      for x in segments if x.text.strip() and x.end > x.start]
    finally:
        audio.unlink(missing_ok=True)
    if not transcript:
        raise ValueError('音声から会話を検出できませんでした')
    candidates = []
    emphatic = ('なぜ', 'なんで', '本当', '実は', 'まさか', 'でも', 'いや', '絶対', '問題',
                'おかしい', '違う', 'すごい', 'びっくり', 'どうして', 'つまり', '結局', '？', '?', '！', '!')
    for i, (begin, _, _) in enumerate(transcript):
        for length in (30, 35, 40, 45):
            if begin + length > duration:
                continue
            included = [x for x in transcript[i:] if x[0] < begin + length and x[1] > begin]
            if not included:
                continue
            words = ''.join(x[2] for x in included)
            spoken = sum(max(0, min(x[1], begin+length)-max(x[0], begin)) for x in included)
            density = spoken / length
            if density < .45 or len(words) < 35:
                continue
            # Penalize speech cut at the end; prefer gaps between turns.
            last_end = included[-1][1]
            boundary = min(abs(last_end-(begin+length)), 4)
            signals = sum(words.count(w) for w in emphatic)
            score = density * 4 + min(signals, 8) * .38 + min(len(words)/length, 5) * .18 - boundary * .25
            # Avoid very first/last moments of the source unless transcript naturally starts there.
            if begin < 4:
                score -= .5
            quote = next((x[2] for x in included if len(x[2]) >= 9 and any(w in x[2] for w in emphatic)), '')
            if not quote:
                quote = max((x[2] for x in included), key=len)
            quote = re.sub(r'[\r\n\t]', '', quote).strip('、。 　')
            if len(quote) > 22:
                quote = quote[:21] + '…'
            headline = ('注目発言｜' + quote)[:26]
            candidates.append((score, begin, length, headline))
    candidates.sort(reverse=True)
    chosen = []
    for score, begin, length, headline in candidates:
        if all(begin + length + 5 <= old[0] or old[0] + old[1] + 5 <= begin
               for old in chosen):
            chosen.append((begin, length, headline))
        if len(chosen) == count:
            break
    if len(chosen) < count:
        raise ValueError(f'重複しない候補が{len(chosen)}本しか見つかりません。長い動画を使うか生成本数を減らしてください')
    return [(headline, int(begin), length) for begin, length, headline in chosen]


@app.post('/batch')
def batch():
    if not authorized():
        abort(401)
    if request.form.get('licensed') != 'on':
        abort(400, '素材の利用権限を確認してください')
    upload = request.files.get('video')
    if not upload or not upload.filename or Path(upload.filename).suffix.lower() not in ('.mp4', '.mov'):
        return page(error='MP4またはMOVをアップロードしてください'), 400
    try:
        count = int(request.form.get('count', '4'))
        if count not in (3, 4, 5):
            raise ValueError('生成本数は3〜5本です')
        mode = request.form.get('caption_mode', 'original')
        if mode not in ('original', 'auto'):
            raise ValueError('字幕設定が不正です')
        specs = []
        manual = request.form.get('manual') == 'on'
        for i in range(1, count + 1) if manual else ():
            headline = request.form.get(f'headline_{i}', '').strip()
            start_raw = request.form.get(f'start_{i}', '').strip()
            length = int(request.form.get(f'length_{i}', '35'))
            if not headline or not start_raw or len(headline) > 26:
                raise ValueError(f'{i}本目の見出しと開始秒数を入力してください（見出し26文字以内）')
            if length not in (30, 35, 40, 45) or int(start_raw) < 0:
                raise ValueError(f'{i}本目の開始秒数・長さが不正です')
            specs.append((headline, int(start_raw), length))
    except (ValueError, TypeError) as exc:
        return page(error=str(exc)), 400
    job = ROOT / uuid.uuid4().hex
    job.mkdir(mode=0o700)
    source = job / 'source.mp4'
    results, warnings = [], []
    try:
        upload.save(source)
        if source.stat().st_size > MAX_JOBS_BYTES:
            raise ValueError('ファイルが大きすぎます')
        duration = media_duration(source)
        if not manual:
            specs = select_highlights(source, duration, count)
        for i, (headline, start, length) in enumerate(specs, 1):
            if start + length > duration:
                warnings.append(f'{i}本目：元動画の終了時刻を超えるためスキップしました')
                continue
            folder = ROOT / uuid.uuid4().hex
            folder.mkdir(mode=0o700)
            try:
                pos, seconds, _ = create_clip(source, folder, length, start, headline, mode)
                results.append(dict(id=folder.name, headline=headline, start=pos, length=seconds))
            except Exception as exc:
                app.logger.warning('Clip %d failed: %s', i, str(exc)[:500])
                warnings.append(f'{i}本目の生成に失敗しました。Renderのログを確認してください')
                shutil.rmtree(folder, ignore_errors=True)
        if not results:
            return page(error='生成できませんでした。動画形式、開始位置、Renderのログを確認してください', warnings=warnings), 422
        return page(results=results, warnings=warnings)
    except Exception as exc:
        app.logger.warning('Batch failed: %s', str(exc)[:500])
        return page(error=str(exc) if isinstance(exc, ValueError) else 'アップロード・文字起こし・動画の処理に失敗しました。Renderログを確認してください'), 422
    finally:
        shutil.rmtree(job, ignore_errors=True)


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
