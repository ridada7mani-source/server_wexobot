#!/usr/bin/env python3
"""Wexo Enhancer Telegram bot + localhost pairing endpoint.

Setup:
  Windows PowerShell: $env:WEXO_BOT_TOKEN="TOKEN_FROM_BOTFATHER"; $env:WEXO_BOT_USERNAME="wexosystem_bot"; python wexo_telegram_bot.py
Video download needs:  pip install yt-dlp   (and ffmpeg for best quality merging)
"""
import json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = os.environ.get('WEXO_BOT_TOKEN', '').strip()
BOT_USERNAME = os.environ.get('WEXO_BOT_USERNAME', 'wexosystem_bot').strip().lstrip('@')
ADMIN_CODE = os.environ.get('WEXO_ADMIN_CODE', '20112008pro')
PREMIUM_URL = 'https://t.me/wexo_all'
SUPPORT_URL = 'https://t.me/wexo_all'
# Where the pairing service listens. Local PC: 127.0.0.1:8765. On a server / hosting: set WEXO_HOST=0.0.0.0 (PORT is read automatically).
# Render sets PORT; bind publicly there. Local Windows remains localhost by default.
PORT = int(os.environ.get('PORT') or os.environ.get('WEXO_PORT') or 8765)
HOST = os.environ.get('WEXO_HOST', '0.0.0.0' if os.environ.get('PORT') else '127.0.0.1').strip() or '127.0.0.1'
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '').strip().rstrip('/')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '').strip()
FFMPEG_PATH: str | None = None  # filled by ensure_ffmpeg() when ffmpeg is not installed system-wide
API = f'https://api.telegram.org/bot{TOKEN}' if TOKEN else ''
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, 'wexo_users.json')
MAX_UPLOAD = 49 * 1024 * 1024  # Telegram bot upload limit is 50 MB
LOCK = threading.Lock()
SESSIONS: dict[str, dict[str, Any]] = {}
VIDEO_ACTIONS: dict[str, dict[str, Any]] = {}  # short-lived metadata for video action buttons
PENDING: dict[str, dict[str, Any]] = {}   # nonce -> {'user_id':..., 'profile':...}
OFFSET = 0

KEYBOARD = {'keyboard': [[{'text': 'Start'}, {'text': 'Download'}, {'text': 'Premium'}]], 'resize_keyboard': True, 'is_persistent': True}

# ---------------------------------------------------------------- storage
def load_db() -> dict:
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception:
        return {'users': {}, 'admins': []}

def save_db(db: dict) -> None:
    with open(DB_FILE, 'w', encoding='utf-8') as f: json.dump(db, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------- telegram helpers
def api(method: str, payload: dict[str, Any] | None = None) -> Any:
    if not API: raise RuntimeError('Set WEXO_BOT_TOKEN first.')
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(API + '/' + method, data=body, headers={'Content-Type': 'application/json'})
    # Slow / unstable connection to Telegram: retry a few times before giving up (HTTP errors from Telegram are not retried).
    last: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=40) as response: return json.loads(response.read().decode())
        except urllib.error.HTTPError: raise
        except Exception as exc:
            last = exc
            if method == 'getUpdates': raise
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]

def btn(text: str, data: str | None = None, url: str | None = None, style: str | None = None) -> dict:
    b: dict[str, Any] = {'text': text}
    if url: b['url'] = url
    if data: b['callback_data'] = data
    if style: b['style'] = style  # 'success' = green, 'danger' = red, 'primary' = blue
    return b

# ---------------------------------------------------------------- Telegram (animated / custom) emoji
# Custom emoji are used when available; otherwise the normal emoji are shown, so nothing ever breaks.
# Telegram only lets bots send custom emoji if the bot owns an extra @username bought on Fragment.
# Source of the emoji (any of them):  WEXO_EMOJI_SETS=packname1,packname2   or a file  wexo_emoji.json  {"❤️": "emoji_id"}
EMOJI_MAP: dict[str, str] = {}
_EMOJI_PAT: Any = None
EMOJI_FILE = os.path.join(BASE_DIR, 'wexo_emoji.json')

def load_custom_emoji() -> None:
    global _EMOJI_PAT
    try:
        with open(EMOJI_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
        if isinstance(data, dict): EMOJI_MAP.update({str(k).replace('\ufe0f', ''): str(v) for k, v in data.items() if v})
    except Exception: pass
    for name in [n.strip() for n in os.environ.get('WEXO_EMOJI_SETS', '').split(',') if n.strip()]:
        try:
            for st in api('getStickerSet', {'name': name}).get('result', {}).get('stickers', []):
                if st.get('custom_emoji_id') and st.get('emoji'): EMOJI_MAP.setdefault(st['emoji'].replace('\ufe0f', ''), st['custom_emoji_id'])
        except Exception as exc: print('Emoji pack skipped:', name, str(exc)[:120])
    if EMOJI_MAP:
        keys = sorted(EMOJI_MAP, key=len, reverse=True)
        _EMOJI_PAT = re.compile('(' + '|'.join(re.escape(k) for k in keys) + ')\ufe0f?')
        print(f'Custom Telegram emoji ready: {len(EMOJI_MAP)}')

def emojify(html_text: str) -> str:
    if not _EMOJI_PAT: return html_text
    parts = re.split(r'(<[^>]*>)', html_text)
    for i, part in enumerate(parts):
        if part.startswith('<'): continue
        parts[i] = _EMOJI_PAT.sub(lambda m: f'<tg-emoji emoji-id="{EMOJI_MAP[m.group(1)]}">{m.group(0)}</tg-emoji>', part)
    return ''.join(parts)

def rich(text: str, parse_mode: str | None = None) -> tuple[str, str | None]:
    """Returns (text, parse_mode) with custom emoji applied when they are available."""
    if not _EMOJI_PAT: return text, parse_mode
    if parse_mode != 'HTML': text = html_escape(text)
    return emojify(text), 'HTML'

def _send_with_fallback(method: str, base: dict, text: str, parse_mode: str | None) -> None:
    attempts = [rich(text, parse_mode)]
    if attempts[0] != (text, parse_mode): attempts.append((text, parse_mode))  # plain emoji if custom ones are refused
    for i, (t, pm) in enumerate(attempts):
        data = {**base, 'text': t}
        if pm: data['parse_mode'] = pm
        try: api(method, data); return
        except Exception:
            if i == len(attempts) - 1: raise

def send_message(chat_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = None) -> None:
    base: dict[str, Any] = {'chat_id': chat_id}
    if reply_markup is not None: base['reply_markup'] = reply_markup
    _send_with_fallback('sendMessage', base, text, parse_mode)

def edit_message(chat_id: int, message_id: int, text: str) -> None:
    try: _send_with_fallback('editMessageText', {'chat_id': chat_id, 'message_id': message_id}, text, None)
    except Exception: pass

def send_video_file(chat_id: int, path: str, caption: str) -> None:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (('chat_id', str(chat_id)), ('caption', caption), ('supports_streaming', 'true')):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    with open(path, 'rb') as f: content = f.read()
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="{os.path.basename(path)}"\r\nContent-Type: video/mp4\r\n\r\n'.encode() + content + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    req = urllib.request.Request(API + '/sendVideo', data=b''.join(parts), headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    urllib.request.urlopen(req, timeout=600).read()

def answer_cb(cb_id: str, text: str = '') -> None:
    try: api('answerCallbackQuery', {'callback_query_id': cb_id, 'text': text})
    except Exception: pass

# ---------------------------------------------------------------- video download
URL_RE = re.compile(r'https?://[^\s<>]+', re.I)
URL_TRAILING = '.,!?;:)]}»”\'"'
ACTIONS_FILE = os.path.join(BASE_DIR, 'wexo_actions.json')
ACTION_TTL = 24 * 3600  # video buttons stay usable for 24 hours (also after a bot restart)

def clean_url(url: str) -> str:
    url = (url or '').strip().strip('<>')
    while url and url[-1] in URL_TRAILING:
        url = url[:-1]
    return url

def html_escape(value: str) -> str:
    import html
    return html.escape(str(value), quote=False)

def human_size(size: Any) -> str:
    try:
        n = float(size)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if n < 1024 or unit == 'GB': return f'{n:.1f} {unit}'
            n /= 1024
    except Exception: pass
    return 'Unknown'

# ---- short-lived storage for the MP3 / Check buttons (survives bot restarts)
def _load_actions() -> dict:
    try:
        with open(ACTIONS_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_actions() -> None:
    try:
        now = time.time()
        for k, v in list(VIDEO_ACTIONS.items()):
            if now - v.get('created', 0) > ACTION_TTL: VIDEO_ACTIONS.pop(k, None)
        with open(ACTIONS_FILE, 'w', encoding='utf-8') as f: json.dump(VIDEO_ACTIONS, f, ensure_ascii=False)
    except Exception as exc:
        print('Could not save button cache:', exc)

def remember_action(url: str, info: dict) -> str:
    key = uuid.uuid4().hex[:10]
    with LOCK:
        VIDEO_ACTIONS[key] = {'url': url, 'info': info, 'created': time.time()}
        _save_actions()
    return key

def get_action(key: str) -> dict | None:
    item = VIDEO_ACTIONS.get(key)
    if item and time.time() - item.get('created', 0) <= ACTION_TTL: return item
    return None

def video_keyboard(key: str, url: str = '') -> dict:
    # Row 1: MP3 | Check (blue)   Row 2: Open + arrow (green, link to the video)   Row 3: @wexo_all (glass / default style)
    rows = [[btn('🎵 MP3', f'mp3:{key}', style='primary'), btn('🔎 Check', f'check:{key}', style='primary')]]
    if url and url.lower().startswith(('http://', 'https://')): rows.append([btn('Open ↗️', url=url, style='success')])
    rows.append([btn('@wexo_all', url=SUPPORT_URL)])
    return {'inline_keyboard': rows}

# ---- high quality TikTok download -------------------------------------------------
# Best resolution first, then H.264 (plays everywhere in Telegram), then bitrate / fps.
# TikTok's own "download" format is the watermarked copy, so it is excluded from the first choice.
HQ_SORT = ['res', 'vcodec:h264', 'br', 'fps']
NO_WM = '[format_id!=download]'
FORMAT_ATTEMPTS = (
    (f'bv*{NO_WM}+ba/b{NO_WM}/bv*+ba/b', HQ_SORT),                          # 1) best quality, no watermark
    ('bv*[vcodec^=avc1]+ba/b[vcodec^=avc1]/bv*[vcodec^=h264]+ba/b', HQ_SORT),  # 2) H.264 only (compatibility)
    ('b[filesize<49M]/b[filesize_approx<49M]/best[ext=mp4]/best', ['res', 'br']),  # 3) fit Telegram's 50 MB limit
    ('worst[ext=mp4]/worst', None),                                          # 4) last resort
)
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'

def _download(url: str, outdir: str, fmt: str, sort: list | None = None) -> tuple[str | None, dict]:
    import yt_dlp
    opts: dict[str, Any] = {'format': fmt, 'outtmpl': os.path.join(outdir, '%(id)s.%(ext)s'), 'noplaylist': True,
            'quiet': True, 'no_warnings': True, 'merge_output_format': 'mp4',
            'retries': 3, 'fragment_retries': 3, 'extractor_retries': 3, 'socket_timeout': 30,
            'http_headers': {'User-Agent': UA, 'Referer': 'https://www.tiktok.com/'}}
    if sort: opts['format_sort'] = sort
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
        path = ydl.prepare_filename(info)
        if os.path.exists(path): return path, info
        base = os.path.splitext(path)[0]
        for ext in ('.mp4', '.mkv', '.webm', '.mov'):
            if os.path.exists(base + ext): return base + ext, info
        # yt-dlp may choose a different final filename after merging.
        candidates = [os.path.join(outdir, f) for f in os.listdir(outdir) if os.path.isfile(os.path.join(outdir, f))]
        return (max(candidates, key=os.path.getsize) if candidates else None), info

def probe_file(path: str) -> dict[str, Any]:
    """Real width/height/fps/codec/bitrate of the file that is actually sent (needs ffprobe, optional)."""
    out: dict[str, Any] = {}
    if not shutil.which('ffprobe'): return out
    try:
        import subprocess
        raw = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', path],
                             capture_output=True, text=True, timeout=30).stdout
        data = json.loads(raw or '{}')
        for s in data.get('streams', []):
            if s.get('codec_type') == 'video':
                out['width'], out['height'] = s.get('width'), s.get('height')
                out['vcodec'] = s.get('codec_name')
                rate = s.get('avg_frame_rate') or s.get('r_frame_rate') or '0/1'
                try:
                    a, b = rate.split('/'); out['fps'] = round(float(a) / float(b), 2) if float(b) else None
                except Exception: pass
                break
        fmt = data.get('format', {})
        if fmt.get('bit_rate'): out['bitrate_kbps'] = round(int(fmt['bit_rate']) / 1000)
        if fmt.get('duration'): out['duration'] = float(fmt['duration'])
    except Exception as exc:
        print('ffprobe failed:', type(exc).__name__, str(exc)[:120])
    return out

def build_info(url: str, raw: dict, path: str) -> dict[str, Any]:
    """Keep only what the Check button needs (small, JSON-safe)."""
    info: dict[str, Any] = {
        'url': url, 'webpage_url': raw.get('webpage_url') or url, 'id': raw.get('id'),
        'title': (raw.get('title') or raw.get('description') or 'TikTok video'),
        'uploader': raw.get('uploader') or raw.get('channel') or 'Unknown', 'uploader_id': raw.get('uploader_id') or raw.get('channel'),
        'duration': raw.get('duration'), 'views': raw.get('view_count'), 'likes': raw.get('like_count'),
        'comments': raw.get('comment_count'), 'shares': raw.get('repost_count'), 'saves': raw.get('save_count'),
        'followers': raw.get('channel_follower_count') or raw.get('uploader_follower_count'),
        'timestamp': raw.get('timestamp'), 'upload_date': raw.get('upload_date'),
        'width': raw.get('width'), 'height': raw.get('height'), 'fps': raw.get('fps'),
        'vcodec': raw.get('vcodec'), 'acodec': raw.get('acodec'), 'tbr': raw.get('tbr'),
        'format_id': raw.get('format_id'),
        'track': raw.get('track'), 'artists': raw.get('artists') or raw.get('artist'),
        'region': raw.get('location') or raw.get('region') or raw.get('country'),
        'downloads': raw.get('download_count'),
    }
    for k, v in scrape_tiktok_page(info['webpage_url']).items():  # public page data fills what yt-dlp did not give
        if v not in (None, '') and info.get(k) in (None, ''): info[k] = v
    info.update({k: v for k, v in probe_file(path).items() if v})  # the real file wins over metadata
    if path and os.path.exists(path): info['file_size'] = os.path.getsize(path)
    return info

def quality_label(info: dict[str, Any]) -> str:
    try:
        short = min(int(info.get('width') or 0), int(info.get('height') or 0)) or int(info.get('height') or 0)
    except Exception: short = 0
    if not short: return 'Unknown'
    name = '4K' if short >= 2000 else '2K' if short >= 1400 else 'Full HD' if short >= 1000 else 'HD' if short >= 700 else 'SD'
    return f'{short}p · {name}'

def shadow_ban_status(info: dict[str, Any]) -> str:
    """Estimate only: TikTok exposes no 'shadow ban' flag. Compares reach with account size, age and engagement."""
    views, followers = info.get('views'), info.get('followers')
    likes = info.get('likes') or 0
    if not isinstance(views, int): return '❔ Not enough public data'
    age_days = None
    if isinstance(info.get('timestamp'), (int, float)): age_days = (time.time() - info['timestamp']) / 86400
    if age_days is not None and age_days < 1: return '⏳ Too new to judge (wait 24h)'
    if isinstance(followers, int) and followers >= 100:
        ratio = views / followers
        if ratio < 0.02 and views < 1000: return '🚫 Likely shadow banned (reach is far below your audience)'
        if ratio < 0.08: return '⚠️ Possibly limited (reach is low for your audience)'
        return '✅ No shadow ban signs'
    if age_days is not None and age_days >= 2 and views < 200:
        return '⚠️ Possibly limited (very low views after 2+ days)'
    if views >= 500 and likes / max(views, 1) < 0.002: return '⚠️ Low engagement for this reach'
    return '✅ No shadow ban signs'

COUNTRIES = {'AE': 'UAE', 'AR': 'Argentina', 'AT': 'Austria', 'AU': 'Australia', 'BD': 'Bangladesh', 'BE': 'Belgium', 'BH': 'Bahrain', 'BR': 'Brazil',
             'CA': 'Canada', 'CH': 'Switzerland', 'CL': 'Chile', 'CN': 'China', 'CO': 'Colombia', 'DE': 'Germany', 'DK': 'Denmark', 'DZ': 'Algeria',
             'EG': 'Egypt', 'ES': 'Spain', 'FI': 'Finland', 'FR': 'France', 'GB': 'United Kingdom', 'GR': 'Greece', 'HK': 'Hong Kong', 'ID': 'Indonesia',
             'IE': 'Ireland', 'IL': 'Israel', 'IN': 'India', 'IQ': 'Iraq', 'IR': 'Iran', 'IT': 'Italy', 'JO': 'Jordan', 'JP': 'Japan', 'KR': 'South Korea',
             'KW': 'Kuwait', 'LB': 'Lebanon', 'LY': 'Libya', 'MA': 'Morocco', 'MX': 'Mexico', 'MY': 'Malaysia', 'NG': 'Nigeria', 'NL': 'Netherlands',
             'NO': 'Norway', 'NZ': 'New Zealand', 'OM': 'Oman', 'PE': 'Peru', 'PH': 'Philippines', 'PK': 'Pakistan', 'PL': 'Poland', 'PS': 'Palestine',
             'PT': 'Portugal', 'QA': 'Qatar', 'RO': 'Romania', 'RU': 'Russia', 'SA': 'Saudi Arabia', 'SD': 'Sudan', 'SE': 'Sweden', 'SG': 'Singapore',
             'SY': 'Syria', 'TH': 'Thailand', 'TN': 'Tunisia', 'TR': 'Turkey', 'TW': 'Taiwan', 'UA': 'Ukraine', 'US': 'United States', 'VN': 'Vietnam',
             'YE': 'Yemen', 'ZA': 'South Africa'}
MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']

def region_text(value: Any) -> str:
    v = str(value or '').strip()
    if not v: return 'Unknown'
    if len(v) == 2 and v.isalpha():
        code = v.upper()
        return f"{''.join(chr(0x1F1E6 + ord(c) - 65) for c in code)} {COUNTRIES.get(code, code)}"
    return v

def scrape_tiktok_page(url: str) -> dict[str, Any]:
    """Reads the public TikTok page for data yt-dlp does not always return (region, music, counters). Never raises."""
    out: dict[str, Any] = {}
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})
        with urllib.request.urlopen(req, timeout=12) as response: page = response.read(4_000_000).decode('utf-8', 'replace')
        m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', page, re.S)
        if not m: return out
        scope = json.loads(m.group(1)).get('__DEFAULT_SCOPE__', {})
        item = ((scope.get('webapp.video-detail') or {}).get('itemInfo') or {}).get('itemStruct') or {}
        stats = {**(item.get('stats') or {}), **(item.get('statsV2') or {})}
        def n(v: Any) -> int | None:
            try: return int(v)
            except Exception: return None
        music = item.get('music') or {}
        out.update({'views': n(stats.get('playCount')), 'likes': n(stats.get('diggCount')), 'comments': n(stats.get('commentCount')),
                    'shares': n(stats.get('shareCount')), 'saves': n(stats.get('collectCount')), 'downloads': n(stats.get('downloadCount')),
                    'region': item.get('locationCreated'), 'track': music.get('title'), 'artists': music.get('authorName')})
    except Exception as exc:
        print('TikTok page read skipped:', type(exc).__name__, str(exc)[:120])
    return out

def shadow_ban_short(info: dict[str, Any]) -> str:
    status = shadow_ban_status(info)
    return 'No' if status.startswith('✅') else 'Yes' if status.startswith('🚫') else 'Maybe' if status.startswith('⚠') else 'Too new' if status.startswith('⏳') else 'Unknown'

def format_check(info: dict[str, Any]) -> str:
    """The Check message (HTML). Layout requested by the Wexo team."""
    esc = html_escape
    def num(v: Any) -> str: return f'{v:,}' if isinstance(v, int) else '—'
    handle = str(info.get('uploader') or '').strip()
    if not handle or handle.isdigit():
        m = re.search(r'/@([^/?#]+)', str(info.get('webpage_url') or info.get('url') or ''))
        handle = m.group(1) if m else (handle or 'unknown')
    ts = info.get('timestamp'); date_text = 'Unknown'
    if isinstance(ts, (int, float)):
        t = time.gmtime(ts); date_text = f'{t.tm_mday} {MONTHS[t.tm_mon - 1]} {t.tm_year}'
    elif info.get('upload_date') and len(str(info['upload_date'])) == 8:
        u = str(info['upload_date']); date_text = f'{int(u[6:8])} {MONTHS[int(u[4:6]) - 1]} {u[:4]}'
    d = info.get('duration'); dur = f'{int(d)//60}:{int(d)%60:02d}' if isinstance(d, (int, float)) else '—'
    track = str(info.get('track') or '').strip() or f'original sound - {handle}'
    artist = str(info.get('artists') or '').strip()
    if artist and artist.lower() not in track.lower(): track = f'{track} - {artist}'
    try: short = min(int(info.get('width') or 0), int(info.get('height') or 0)) or int(info.get('height') or 0)
    except Exception: short = 0
    fps = info.get('fps'); fps_t = f' {round(float(fps))} fps' if isinstance(fps, (int, float)) and fps else ''
    w, h = info.get('width'), info.get('height')
    size_mb = f"{info['file_size'] / 1048576:.1f}Mb" if info.get('file_size') else 'Unknown'
    lines = ['📹 Video Statistics', '',
             f'🎵@{esc(handle)}', '',
             f'👉 {date_text}', '',
             f'🎶- {esc(track)} • {dur}', '',
             '📊 Statistics',
             f"• 👁 {num(info.get('views'))} views",
             f"• ❤️ {num(info.get('likes'))} Likes",
             f"• 💬 {num(info.get('comments'))} Comments",
             f"• 🔖 {num(info.get('saves'))} Favorites",
             f"• ➡️ {num(info.get('shares'))} Shares",
             f"• ⬇️ {num(info.get('downloads'))} Downloads", '',
             '💬 Information',
             f"• 🌍 Region | {esc(region_text(info.get('region')))}",
             f'• 🛡 Shadow ban | {shadow_ban_short(info)}', '',
             '🎥 Quality',
             f"• 📰Video | {f'{short}p' if short else 'Unknown'}{fps_t}",
             f'• 📁 File size | {size_mb}',
             f"• Original | {f'{w}×{h}' if w and h else 'Unknown'}", '',
             '⚡ Powered By Wexo',
             '🎧@wexo_all']
    return '\n'.join(lines)

def send_local_file(chat_id: int, path: str, caption: str = '', field: str = 'video', mime: str = 'video/mp4',
                    reply_markup: dict | None = None, extra: dict[str, Any] | None = None, _plain: bool = False) -> None:
    boundary = uuid.uuid4().hex
    fields: list[tuple[str, str]] = [('chat_id', str(chat_id))]
    cap, pm = (caption, None) if _plain else rich(caption, None)
    if cap: fields.append(('caption', cap))
    if pm: fields.append(('parse_mode', pm))
    if field == 'video': fields.append(('supports_streaming', 'true'))
    if reply_markup: fields.append(('reply_markup', json.dumps(reply_markup)))
    for k, v in (extra or {}).items():
        if v not in (None, ''): fields.append((k, str(v)))
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields]
    with open(path, 'rb') as f: content = f.read()
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{os.path.basename(path)}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + content + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    endpoint = {'video': '/sendVideo', 'audio': '/sendAudio', 'document': '/sendDocument'}[field]
    req = urllib.request.Request(API + endpoint, data=b''.join(parts), headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        try:
            with urllib.request.urlopen(req, timeout=600) as response: result = json.loads(response.read().decode())
        except urllib.error.HTTPError: raise
        except Exception:
            time.sleep(2)
            with urllib.request.urlopen(req, timeout=600) as response: result = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if pm and not _plain:  # custom emoji refused -> resend with normal emoji
            return send_local_file(chat_id, path, caption, field, mime, reply_markup, extra, _plain=True)
        raise RuntimeError('Telegram rejected media: ' + exc.read().decode(errors='replace')[:200])
    if not result.get('ok'): raise RuntimeError('Telegram rejected uploaded media')

def download_and_send(chat_id: int, url: str) -> None:
    tmp = tempfile.mkdtemp(prefix='wexo_')
    try:
        try: import yt_dlp  # noqa: F401
        except ImportError:
            send_message(chat_id, '⚠️ Downloader missing on the bot computer. Run: pip install -U yt-dlp', KEYBOARD); return
        path, raw = None, {}
        for fmt, sort in FORMAT_ATTEMPTS:
            try:
                path, raw = _download(url, tmp, fmt, sort)
                if path and os.path.exists(path) and os.path.getsize(path) <= MAX_UPLOAD: break
                if path and os.path.exists(path): os.remove(path)  # too big for Telegram -> try the next, lighter format
                path = None
            except Exception as exc:
                print('Video format attempt failed:', type(exc).__name__, str(exc)[:240])
                path = None
        if not path:
            send_message(chat_id, '❌ لم أستطع تنزيل هذا الفيديو. قد يكون خاصًا أو محذوفًا أو محجوبًا مؤقتًا، أو يتجاوز حد تيليجرام.', KEYBOARD); return
        info = build_info(url, raw, path)
        key = remember_action(url, info)
        extra = {'width': info.get('width'), 'height': info.get('height'),
                 'duration': int(info['duration']) if isinstance(info.get('duration'), (int, float)) else None}
        # The video on top; under it: MP3 | Check, then Open (green), then @wexo_all.
        send_local_file(chat_id, path, '✅ Wexo Enhancer', 'video',
                        'video/mp4' if path.lower().endswith('.mp4') else 'application/octet-stream',
                        reply_markup=video_keyboard(key, info.get('webpage_url') or url), extra=extra)
    except Exception as exc:
        print('Video download/send failed:', type(exc).__name__, str(exc)[:300])
        send_message(chat_id, '❌ تعذّر تنزيل الفيديو أو إرساله. حدّث yt-dlp وتأكد من تثبيت FFmpeg، ثم جرّب الرابط العام مرة أخرى.', KEYBOARD)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def ensure_ffmpeg() -> None:
    """MP3 needs FFmpeg. If it is not installed, use the bundled one from the imageio-ffmpeg package (installed automatically)."""
    global FFMPEG_PATH
    if shutil.which('ffmpeg'): return
    for attempt in range(2):
        try:
            import imageio_ffmpeg
            FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe(); print('Using bundled FFmpeg:', FFMPEG_PATH); return
        except Exception:
            if attempt == 0:
                try: subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '--disable-pip-version-check', 'imageio-ffmpeg'], timeout=300)
                except Exception as exc: print('Could not install imageio-ffmpeg:', exc)
    print('FFmpeg not found: MP3 will be sent as the original audio file instead.')

def download_mp3(chat_id: int, key: str) -> None:
    item = get_action(key)
    if not item: send_message(chat_id, 'انتهت صلاحية هذا الزر. أرسل رابط الفيديو مرة أخرى.', KEYBOARD); return
    tmp = tempfile.mkdtemp(prefix='wexo_mp3_')
    info = item.get('info') or {}
    meta = {'title': str(info.get('title') or 'TikTok audio')[:60], 'performer': str(info.get('uploader') or '')[:60],
            'duration': int(info['duration']) if isinstance(info.get('duration'), (int, float)) else None}
    def base_opts(**more: Any) -> dict[str, Any]:
        o: dict[str, Any] = {'outtmpl': os.path.join(tmp, '%(id)s.%(ext)s'), 'noplaylist': True, 'quiet': True, 'no_warnings': True,
                             'retries': 3, 'extractor_retries': 3, 'socket_timeout': 30,
                             'http_headers': {'User-Agent': UA, 'Referer': 'https://www.tiktok.com/'}}
        if FFMPEG_PATH: o['ffmpeg_location'] = FFMPEG_PATH
        o.update(more); return o
    try:
        import yt_dlp
        # 1) real MP3 (needs FFmpeg: installed one or the bundled one)
        try:
            opts = base_opts(format='bestaudio/best', postprocessors=[{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}])
            with yt_dlp.YoutubeDL(opts) as ydl: ydl.extract_info(item['url'], download=True)
            files = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.lower().endswith('.mp3')]
            if files:
                send_local_file(chat_id, files[0], '🎵 MP3 · Wexo Enhancer', 'audio', 'audio/mpeg', extra=meta); return
        except Exception as exc:
            print('MP3 conversion failed, trying original audio:', type(exc).__name__, str(exc)[:200])
        # 2) no FFmpeg: send the original audio track (M4A plays as audio in Telegram)
        for f in os.listdir(tmp): os.remove(os.path.join(tmp, f))
        with yt_dlp.YoutubeDL(base_opts(format='bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best')) as ydl: ydl.extract_info(item['url'], download=True)
        files = [os.path.join(tmp, f) for f in os.listdir(tmp) if os.path.isfile(os.path.join(tmp, f))]
        if not files: raise RuntimeError('No audio produced')
        audio = max(files, key=os.path.getsize)
        if os.path.getsize(audio) > MAX_UPLOAD: raise RuntimeError('Audio too large for Telegram')
        if audio.lower().endswith(('.mp3', '.m4a')):
            send_local_file(chat_id, audio, '🎵 MP3 · Wexo Enhancer', 'audio', 'audio/mpeg' if audio.lower().endswith('.mp3') else 'audio/mp4', extra=meta)
        else:
            send_local_file(chat_id, audio, '🎵 Audio · Wexo Enhancer', 'document', 'application/octet-stream')
    except Exception as exc:
        print('MP3 download failed:', type(exc).__name__, str(exc)[:240])
        send_message(chat_id, '⚠️ تعذّر إنشاء MP3. تأكد من تثبيت FFmpeg على جهاز تشغيل البوت.', KEYBOARD)
    finally: shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- admin panel
def get_photo_id(user_id: int) -> str | None:
    try:
        r = api('getUserProfilePhotos', {'user_id': user_id, 'limit': 1}).get('result', {})
        if r.get('total_count'): return r['photos'][0][-1]['file_id']
    except Exception: pass
    return None

def _fmt_time(ts: Any) -> str:
    try: return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(float(ts)))
    except Exception: return 'Unknown'

def is_premium(u: dict) -> bool:
    return bool(u.get('premium')) or u.get('role') == 'premium'  # 'role: premium' = older bot versions

def user_text(uid: str, u: dict) -> str:
    """All the account information as text (no profile picture)."""
    uname = f"@{html_escape(u['username'])}" if u.get('username') else 'no username'
    premium = is_premium(u)
    lines = [f"👤 <b>{html_escape(u.get('first_name') or 'User')}</b>",
             f'🆔 ID: <code>{html_escape(uid)}</code>',
             f'🔗 Username: {uname}',
             f"⭐ Plan: {'Premium ✅ (open)' if premium else 'Free 🔒 (closed)'}",
             f"🛠 Role: {'Developer' if u.get('role') == 'developer' else 'User'}",
             f"🕒 Last login: {_fmt_time(u.get('at'))}"]
    if u.get('premium_at'): lines.append(f"📅 Premium since: {_fmt_time(u['premium_at'])}")
    return '\n'.join(lines)

def user_markup(uid: str, u: dict) -> dict:
    label = 'Premium ✅' if is_premium(u) else 'Premium'
    return {'inline_keyboard': [[btn(label, f'role:p:{uid}', style='success'), btn('Developer', f'role:d:{uid}', style='primary')]]}

def show_users(chat_id: int) -> None:
    users = load_db()['users']
    if not users:
        send_message(chat_id, 'No registered users yet.'); return
    for uid, u in users.items():
        send_message(chat_id, user_text(uid, u), user_markup(uid, u), parse_mode='HTML')
        time.sleep(0.05)  # stay under Telegram's flood limit

ROLE_NAMES = {'p': 'Premium', 'd': 'Developer'}
ROLE_WELCOME = {
    'p': '🎉 Welcome to Wexo Enhancer Premium!\n\n🚀 Your Premium access is now open.\n💞 Thank you for supporting Wexo Enhancer!',
    'd': '🛠 Welcome to Wexo Enhancer Developer!\n\n🚀 Your Developer access is now open.\n💞 Thank you for being part of Wexo Enhancer!',
}

def is_admin(user_id: int) -> bool:
    return user_id in load_db().get('admins', [])

def handle_callback(cq: dict) -> None:
    data = cq.get('data') or ''
    user = cq.get('from') or {}
    uid = user.get('id')
    msg = cq.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    msg_id = msg.get('message_id')
    answer_cb(cq['id'])
    if not chat_id: return

    if data.startswith('mp3:'):
        download_mp3(chat_id, data.split(':', 1)[1]); return
    if data.startswith('check:'):
        item = get_action(data.split(':', 1)[1])
        if not item: send_message(chat_id, 'انتهت صلاحية بيانات الفيديو. أرسل الرابط مرة أخرى.', KEYBOARD)
        else: send_message(chat_id, format_check(item['info']), parse_mode='HTML')
        return

    if data.startswith('login:yes:') or data.startswith('login:no:') or data in ('login:yes', 'login:no'):
        # Carry the exact nonce in the button callback. This avoids expired/mismatched
        # requests when a user starts a second login or has multiple extension windows open.
        parts = data.split(':', 2)
        action = parts[1] if len(parts) > 1 else ''
        nonce = parts[2] if len(parts) > 2 else ''
        with LOCK:
            pending = PENDING.pop(nonce, None) if nonce else None
            # Backward compatibility for buttons sent by an older running bot process.
            if pending is None and data in ('login:yes', 'login:no'):
                pending = next((v for k, v in list(PENDING.items()) if v.get('user_id') == uid), None)
                if pending:
                    nonce = pending['nonce']
                    PENDING.pop(nonce, None)
        if not pending or pending.get('user_id') != uid:
            send_message(chat_id, 'This login request has expired. Please return to the extension and click Login again.', KEYBOARD); return
        profile = pending['profile']
        if action == 'yes':
            photo = get_photo_id(uid)  # network call kept outside the lock
            with LOCK:
                db = load_db()
                old = db['users'].get(str(uid), {})
                db['users'][str(uid)] = {**old, **profile, 'photo': photo or old.get('photo'), 'at': time.time()}
                save_db(db)
                SESSIONS[nonce] = {'authenticated': True, 'user': profile, 'at': time.time()}
            send_message(chat_id, "✅ You're signed in to Wexo Enhancer — 💞 please go back to the extension.", KEYBOARD)
        else:
            # "No": the extension stays locked and shows that the login was refused.
            with LOCK: SESSIONS[nonce] = {'authenticated': False, 'denied': True, 'at': time.time()}
            send_message(chat_id, '🚫 Login was cancelled. If this was not you, you can ignore it.', KEYBOARD)
        try: api('editMessageReplyMarkup', {'chat_id': chat_id, 'message_id': msg_id, 'reply_markup': {'inline_keyboard': []}})
        except Exception: pass
        return

    if not is_admin(uid): return

    if data.startswith('role:'):
        _, r, target = data.split(':')
        u = load_db()['users'].get(target) or {}
        who = f"@{u['username']}" if u.get('username') else f"{u.get('first_name') or 'User'} ({target})"
        if r == 'p':
            send_message(chat_id, f'هل يريد المؤسس @wexo_all فتح بريميوم لحساب {who} ؟',
                         {'inline_keyboard': [[btn('Unlock', f'pr:y:{target}', style='success'), btn('No unlock', f'pr:n:{target}', style='danger')]]})
        else:
            send_message(chat_id, f'هل يريد المؤسس @wexo_all جعل {who} مطورا ؟',
                         {'inline_keyboard': [[btn('Unlock', f'ok:{r}:{target}', style='success'), btn('No unlock', 'cancel', style='danger')]]})
    elif data.startswith('pr:'):
        _, choice, target = data.split(':')
        notify = False
        with LOCK:
            db = load_db()
            u = db['users'].get(target)
            if u is None:
                result = '❌ User not found.'
            elif choice == 'y':
                notify = not is_premium(u)
                u['premium'] = True; u.setdefault('premium_at', time.time()); save_db(db)
                result = f'✅ Premium is open for ID {target}.'
            else:
                u['premium'] = False; u.pop('premium_at', None)
                if u.get('role') == 'premium': u['role'] = None  # clear the older-style premium flag too
                save_db(db)
                result = f'🔒 Premium is closed for ID {target}.'
        if notify:
            try: send_message(int(target), ROLE_WELCOME['p'], KEYBOARD)
            except Exception: pass
        edit_message(chat_id, msg_id, result)
    elif data.startswith('ok:'):
        _, r, target = data.split(':')
        found = False
        with LOCK:
            db = load_db()
            if target in db['users']:
                db['users'][target]['role'] = ROLE_NAMES[r].lower(); save_db(db); found = True
        if found:
            try: send_message(int(target), ROLE_WELCOME[r], KEYBOARD)
            except Exception: pass
        edit_message(chat_id, msg_id, f'✅ {ROLE_NAMES[r]} opened for ID {target}.')
    elif data == 'cancel':
        edit_message(chat_id, msg_id, '❎ Cancelled.')

# ---------------------------------------------------------------- messages
def handle_message(msg: dict) -> None:
    chat = msg.get('chat') or {}; chat_id = chat.get('id')
    if not chat_id: return
    text = (msg.get('text') or '').strip()
    user = msg.get('from') or {}

    # secret code -> user list (the code message is deleted so it is never visible)
    if text == ADMIN_CODE:
        try: api('deleteMessage', {'chat_id': chat_id, 'message_id': msg['message_id']})
        except Exception: pass
        db = load_db()
        if user.get('id') not in db['admins']:
            db['admins'].append(user.get('id')); save_db(db)
        show_users(chat_id)
        return

    if text.startswith('/start') or text.lower() == 'start':
        parts = text.split(maxsplit=1); payload = parts[1] if len(parts) > 1 else ''
        if payload.startswith('login_'):
            nonce = payload[len('login_'):]
            if 24 <= len(nonce) <= 100 and all(c in '0123456789abcdef' for c in nonce.lower()):
                profile = {'id': user.get('id'), 'username': user.get('username'), 'first_name': user.get('first_name') or 'Wexo User'}
                with LOCK: PENDING[nonce] = {'nonce': nonce, 'user_id': user.get('id'), 'profile': profile}
                send_message(chat_id, '🔐 Are you the one trying to sign in to the Wexo Enhancer extension?',
                             {'inline_keyboard': [[btn('✅ Yes, it\'s me', 'login:yes:' + nonce, style='success')],
                                                  [btn('❌ No, it\'s not me', 'login:no:' + nonce, style='danger')]]})
                return
        send_message(chat_id, '💞 Welcome to Wexo Enhancer!\nChoose Start, Download, or Premium from the menu below.', KEYBOARD)
        return
    if text.lower() == 'download':
        send_message(chat_id, '🎬 Send me a video link and I will send you the video in high quality.', KEYBOARD)
        return
    if text.lower() == 'premium':
        send_message(chat_id, '✨ Wexo Enhancer Premium\n\n🚀 Unlock premium access and extra features.\n🔒 Premium options are managed by the Wexo team.\n💞 Thank you for supporting Wexo Enhancer!',
                     {'inline_keyboard': [[btn('Buy Premium 🛒', url=PREMIUM_URL, style='success')]]})
        return
    m = URL_RE.search(text)
    if m:
        video_url = clean_url(m.group(0))
        send_message(chat_id, '⏳ جاري فحص الرابط وتنزيل الفيديو بأعلى جودة متاحة...')
        threading.Thread(target=download_and_send, args=(chat_id, video_url), daemon=True).start()
        return
    if msg.get('video') or (msg.get('document') and str(msg['document'].get('mime_type', '')).startswith('video/')):
        file = msg.get('video') or msg.get('document')
        kind = 'video' if msg.get('video') else 'document'
        api('sendVideo' if kind == 'video' else 'sendDocument', {'chat_id': chat_id, kind: file['file_id'], 'caption': '✅ Original video returned by Wexo Enhancer.'})
        send_message(chat_id, 'Done 💞 The original Telegram file was returned without re-encoding.', KEYBOARD)
        return
    send_message(chat_id, 'Choose Start, Download, or Premium below. For Download, tap it and then send your video link.', KEYBOARD)

# ---------------------------------------------------------------- local pairing endpoint (unchanged)
class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204); self.send_header('Access-Control-Allow-Origin', '*'); self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS'); self.send_header('Access-Control-Allow-Headers', 'Content-Type'); self.end_headers()
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != '/telegram-webhook' or not WEBHOOK_URL:
            self.send_json(404, {'error': 'not_found'}); return
        if WEBHOOK_SECRET and self.headers.get('X-Telegram-Bot-Api-Secret-Token', '') != WEBHOOK_SECRET:
            self.send_json(403, {'error': 'forbidden'}); return
        try:
            length = min(int(self.headers.get('Content-Length', '0')), 2_000_000)
            update = json.loads(self.rfile.read(length).decode('utf-8'))
            if update.get('message'): handle_message(update['message'])
            elif update.get('callback_query'): handle_callback(update['callback_query'])
            self.send_json(200, {'ok': True})
        except Exception as exc:
            print('Webhook handler error:', exc)
            self.send_json(400, {'error': 'bad_update'})
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path in ('/', '/health'): self.send_json(200, {'ok': True, 'service': 'wexo-bot'}); return
        if parsed.path == '/auth/photo':
            # Serve the authenticated Telegram user's avatar through the local bot service.
            # The bot token remains server-side and is never returned to the extension.
            try:
                uid = int(params.get('user_id', ['0'])[0])
                if uid <= 0: self.send_json(400, {'error': 'invalid_user'}); return
                if str(uid) not in load_db()['users']: self.send_response(404); self.send_header('Access-Control-Allow-Origin', '*'); self.end_headers(); return
                file_id = get_photo_id(uid)
                if not file_id: self.send_response(404); self.send_header('Access-Control-Allow-Origin', '*'); self.end_headers(); return
                file_info = api('getFile', {'file_id': file_id}).get('result', {})
                file_path = file_info.get('file_path')
                if not file_path: self.send_response(404); self.end_headers(); return
                request = urllib.request.Request(f'https://api.telegram.org/file/bot{TOKEN}/{file_path}', headers={'User-Agent': 'Wexo-Enhancer/1.5.4'})
                with urllib.request.urlopen(request, timeout=8) as response: image_data = response.read(5 * 1024 * 1024 + 1)
                if not image_data or len(image_data) > 5 * 1024 * 1024: self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header('Content-Type', 'image/jpeg'); self.send_header('Cache-Control', 'no-store'); self.send_header('Access-Control-Allow-Origin', '*'); self.send_header('Content-Length', str(len(image_data))); self.end_headers(); self.wfile.write(image_data)
            except Exception:
                self.send_response(404); self.end_headers()
            return
        if parsed.path == '/auth/role':
            # The extension asks whether Premium is open for this Telegram user (Yes/No in the admin panel).
            try: uid = str(int(params.get('user_id', ['0'])[0]))
            except Exception: self.send_json(400, {'error': 'invalid_user'}); return
            u = load_db()['users'].get(uid)
            self.send_json(200, {'known': bool(u), 'premium': bool(u and is_premium(u)), 'developer': bool(u and u.get('role') == 'developer')})
            return
        if parsed.path != '/auth/status': self.send_json(404, {'error': 'not_found'}); return
        nonce = params.get('nonce', [''])[0]
        with LOCK:
            record = SESSIONS.get(nonce)
            if record and time.time() - record['at'] > 600: SESSIONS.pop(nonce, None); record = None
        self.send_json(200, record or {'authenticated': False})
    def send_json(self, code, obj):
        data = json.dumps(obj).encode(); self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Access-Control-Allow-Origin', '*'); self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *_): pass

def poll_telegram():
    global OFFSET
    while True:
        try:
            result = api('getUpdates', {'offset': OFFSET, 'timeout': 25, 'allowed_updates': ['message', 'callback_query']})
            for update in result.get('result', []):
                OFFSET = max(OFFSET, update['update_id'] + 1)
                try:
                    if update.get('message'): handle_message(update['message'])
                    elif update.get('callback_query'): handle_callback(update['callback_query'])
                except Exception as exc:
                    print('Handler error:', exc)
        except Exception as exc:
            print('Telegram polling error:', exc); time.sleep(3)

def main():
    if not TOKEN:
        raise SystemExit('Missing WEXO_BOT_TOKEN. Create/configure your bot with BotFather, then set this environment variable.')
    # Optional proxy/VPN for networks where Telegram is slow or blocked:  WEXO_PROXY=http://127.0.0.1:10809
    proxy = os.environ.get('WEXO_PROXY', '').strip()
    if proxy:
        os.environ['HTTPS_PROXY'] = proxy; os.environ['HTTP_PROXY'] = proxy
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy})))
    me: dict = {}
    for attempt in range(1, 1000):  # do not crash when the connection to Telegram is slow: keep retrying
        try:
            me = api('getMe', {}).get('result', {}); break
        except urllib.error.HTTPError as exc:
            if exc.code == 401: raise SystemExit('Invalid bot token (401). Copy the full token from @BotFather again.')
            print(f'Telegram answered {exc.code}. Retrying...'); time.sleep(5)
        except Exception as exc:
            print(f'Cannot reach Telegram (attempt {attempt}): {type(exc).__name__}. Check your internet / VPN. Retrying in 5s...'); time.sleep(5)
    actual = (me.get('username') or '').lower()
    if actual and actual != BOT_USERNAME.lower():
        print(f'Warning: token belongs to @{actual}, but WEXO_BOT_USERNAME is @{BOT_USERNAME}. Update wexo-auth.js BOT_USERNAME to @{actual}.')
    VIDEO_ACTIONS.update(_load_actions())
    load_custom_emoji()
    ensure_ffmpeg()
    if WEBHOOK_URL:
        hook = WEBHOOK_URL + '/telegram-webhook'
        payload = {'url': hook, 'allowed_updates': ['message', 'callback_query']}
        if WEBHOOK_SECRET: payload['secret_token'] = WEBHOOK_SECRET
        result = api('setWebhook', payload)
        if not result.get('ok'): raise SystemExit('Telegram setWebhook failed: ' + str(result))
        print('Telegram webhook configured:', hook)
    else:
        try: api('deleteWebhook', {'drop_pending_updates': False})
        except Exception as exc: print('deleteWebhook skipped:', exc)
        threading.Thread(target=poll_telegram, daemon=True).start()
        print('Telegram polling mode enabled (local development).')
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Wexo Telegram pairing service is running for @{actual or BOT_USERNAME} on http://{HOST}:{PORT}')
    print('Keep this running: the extension signs users in through this service.')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == '__main__': main()
