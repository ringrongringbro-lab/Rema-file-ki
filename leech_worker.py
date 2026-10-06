"""
leech_worker.py — GitHub Actions Worker
=========================================
Yeh script GitHub Actions runner ke andar EK BAAR chalti hai (phone controller
ke dispatch se trigger hoti hai), poora kaam karti hai, phir khatam ho jaati hai:

    Download (aria2c) -> Extract (7z, agar archive hai) -> Upload (Telegram)

Runner ka apna temporary disk use hota hai — job khatam hote hi sab clean ho
jaata hai, isliye koi cleanup code alag se nahi chahiye.

Note: sticker-divider feature (original phone/Colab version mein tha) yahan
nahi hai, kyunki har Actions run ek FRESH machine hai — local JSON file
persist nahi karti run-to-run. Agar sticker chahiye ho to woh baad mein
alag se (GitHub Variable ya secret ke through) add karenge.
"""

import os
import re
import time
import json
import shutil
import asyncio
import requests
import urllib.parse
import io
import subprocess

from pyrogram import Client
import yt_dlp

try:
    from PIL import Image
except Exception:  # Pillow na mile to thumbnail skip ho jayega
    Image = None

# ---------------- ENV VARS (workflow se aate hain) ----------------
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

URL = os.getenv("URL")
RENAME = (os.getenv("RENAME") or "").strip()
UPLOAD_FORMAT = (os.getenv("UPLOAD_FORMAT") or "media").strip().lower()
THUMB_FILE_ID = (os.getenv("THUMB_FILE_ID") or "").strip()
CHAT_ID = int(os.getenv("CHAT_ID"))
TRIGGER_MSG_ID = os.getenv("TRIGGER_MSG_ID", "none")

FAST_DOWNLOAD_DIR = "downloads"
FAST_EXTRACT_DIR = "extracted"
os.makedirs(FAST_DOWNLOAD_DIR, exist_ok=True)
os.makedirs(FAST_EXTRACT_DIR, exist_ok=True)

VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.flv', '.ts', '.3gp')
DOC_EXTS = ('.zip', '.rar', '.7z', '.pdf')
ALLOWED_EXTS = VIDEO_EXTS + DOC_EXTS

SITE_TAG = "@asi_anime"


# ---------------- HELPERS ----------------
def bypass_pixeldrain(url):
    if "pixeldrain." in url and "/u/" in url:
        file_id = url.split("/u/")[1].split("?")[0].split("/")[0]
        return f"https://pixeldrain.com/api/file/{file_id}"
    return url


def get_real_filename(url):
    if "pixeldrain.com/api/file/" in url:
        file_id = url.split("/")[-1]
        try:
            r = requests.get(f"https://pixeldrain.com/api/file/{file_id}/info", timeout=5)
            name = r.json().get("name")
            if name:
                return name
        except Exception:
            pass
    name = urllib.parse.unquote(url.split("/")[-1].split("?")[0])
    if "." not in name:
        name += ".zip"
    return name


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '', s or '').strip().strip('.')
    return s[:150]


def natural_key(path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', path)]


_RES_RE = re.compile(r'\b(?:\d{3,4}x\d{3,4}|(?:480|540|576|720|1080|1440|2160)[pi]|4k|8k)\b', re.I)
_CODEC_RE = re.compile(
    r'\b(?:x26[45]|h\.?26[45]|hevc|avc|av1|aac(?:2\.0)?|ac3|eac3|flac|opus|'
    r'10[\s-]?bit|8[\s-]?bit|\d+(?:\.\d+)?\s?(?:kbps|fps|mb|gb))\b', re.I)
_HASH_RE = re.compile(r'[\[\(][0-9A-Fa-f]{8}[\]\)]')
_YEAR_RE = re.compile(r'[\[\(](?:19|20)\d{2}[\]\)]')


def _clean_name(s):
    s = s.replace('_', ' ')
    s = _HASH_RE.sub(' ', s)
    s = _YEAR_RE.sub(' ', s)
    s = _RES_RE.sub(' ', s)
    s = _CODEC_RE.sub(' ', s)
    s = s.replace('.', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def detect_season_episode(path):
    """File ke naam (aur folder ke naam) se (season, episode) nikalta hai. Na mile to None."""
    base = os.path.splitext(os.path.basename(path))[0]
    folder = os.path.basename(os.path.dirname(path))
    name = _clean_name(base)
    season = episode = None

    m = re.search(r'\bS(\d{1,2})\s?[-_ ]?E(?:P)?\s?(\d{1,4})\b', name, re.I)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r'\b(\d{1,2})x(\d{1,3})\b', name, re.I)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))

    if season is None:
        for text in (name, _clean_name(folder)):
            m = (re.search(r'\bseason\s?(\d{1,2})\b', text, re.I)
                 or re.search(r'\b(\d{1,2})(?:st|nd|rd|th)\s+season\b', text, re.I)
                 or re.search(r'\bS(\d{1,2})\b', text))
            if m:
                season = int(m.group(1))
                break

    if episode is None:
        m = re.search(r'\b(?:episode|ep|e)\s?[-_.]?\s?(\d{1,4})\b', name, re.I)
        if m:
            episode = int(m.group(1))
    if episode is None:
        found = re.findall(r'(?:^|\s)-\s?(\d{1,4})(?:v\d)?(?=\s|$)', name)
        if found:
            episode = int(found[-1])
    if episode is None:
        found = re.findall(r'(?<![\w])(\d{1,3})(?![\w])', name)
        if found:
            episode = int(found[-1])

    return season, episode


def build_final_names(files, came_from_archive):
    """{old_path: new_file_name} aur guess ki gayi files ki list."""
    names, guessed = {}, []
    clean_rename = safe_name(RENAME)
    if not clean_rename:
        return names, guessed

    if len(files) > 1 or came_from_archive:
        detected = {}
        used = {}
        for f in files:
            season, ep = detect_season_episode(f)
            season = 1 if season is None else season
            detected[f] = (season, ep)
            if ep is not None:
                used.setdefault(season, set()).add(ep)

        for idx, f in enumerate(files, 1):
            season, ep = detected[f]
            if ep is None:
                ep = idx
                while ep in used.setdefault(season, set()):
                    ep += 1
                used[season].add(ep)
                guessed.append(f"{os.path.basename(f)} → S{season:02d} EP-{ep:02d}")
            ext = os.path.splitext(f)[1]
            names[f] = f"{SITE_TAG}[S{season:02d}][EP-{ep:02d}]{clean_rename}{ext}"
    else:
        f = files[0]
        ext = os.path.splitext(f)[1]
        names[f] = clean_rename if clean_rename.lower().endswith(ext.lower()) else clean_rename + ext
    return names, guessed


def apply_name(path, new_name):
    if not new_name:
        return path
    folder = os.path.dirname(path)
    new_path = os.path.join(folder, new_name)
    if os.path.abspath(new_path) == os.path.abspath(path):
        return path
    if os.path.exists(new_path):
        base, ext = os.path.splitext(new_name)
        new_path = os.path.join(folder, f"{base}_{int(time.time())}{ext}")
    try:
        os.rename(path, new_path)
        return new_path
    except Exception as e:
        print(f"Rename failed ({path}): {e}")
        return path


def prepare_thumb():
    """Telegram file_id se thumbnail download karke JPEG (<=320px, <200KB) banata hai."""
    if not THUMB_FILE_ID or Image is None:
        return None
    try:
        info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                            params={"file_id": THUMB_FILE_ID}, timeout=15).json()
        file_path = info["result"]["file_path"]
        raw = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=30).content
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((320, 320))
        out = "thumb.jpg"
        quality = 90
        while True:
            img.save(out, "JPEG", quality=quality, optimize=True)
            if os.path.getsize(out) <= 190 * 1024 or quality <= 40:
                break
            quality -= 10
        return out
    except Exception as e:
        print(f"Thumbnail prepare failed: {e}")
        return None


def probe_video(path):
    """ffprobe se duration/width/height (na mile to khaali dict)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout or "{}")
        meta = {}
        stream = (data.get("streams") or [{}])[0]
        if stream.get("width") and stream.get("height"):
            meta["width"] = int(stream["width"])
            meta["height"] = int(stream["height"])
        dur = (data.get("format") or {}).get("duration")
        if dur:
            meta["duration"] = int(float(dur))
        return meta
    except Exception:
        return {}


def is_torrent_source(url):
    u = (url or "").strip()
    return u.lower().startswith("magnet:") or u.split("?")[0].lower().endswith(".torrent")


def list_torrent_files(dest_dir):
    found = []
    for root, _, files in os.walk(dest_dir):
        for file in files:
            if file.lower().endswith(('.torrent', '.aria2')):
                continue
            found.append(os.path.join(root, file))
    return found


# ---------------- STATUS via plain Bot API (no need to wait on Pyrogram) ----------------
status_msg_id = None


def _tg_call(method, **params):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=15)
        return r.json()
    except Exception as e:
        print(f"Telegram API error ({method}): {e}")
        return {}


def send_status(text):
    global status_msg_id
    resp = _tg_call("sendMessage", chat_id=CHAT_ID, text=text)
    status_msg_id = resp.get("result", {}).get("message_id")


def edit_status(text):
    if status_msg_id is None:
        send_status(text)
        return
    _tg_call("editMessageText", chat_id=CHAT_ID, message_id=status_msg_id, text=text)


last_edit = {"t": 0}


def maybe_edit_status(text, min_interval=4):
    now = time.time()
    if now - last_edit["t"] > min_interval:
        edit_status(text)
        last_edit["t"] = now


# ---------------- DOWNLOAD ----------------
async def download_aria2(url, dest_dir, filename):
    url = bypass_pixeldrain(url)
    dest_path = os.path.join(dest_dir, filename)
    cmd = f'aria2c -x 16 -s 16 -k 1M --summary-interval=1 -d "{dest_dir}" -o "{filename}" "{url}"'

    process = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    while True:
        line = await process.stdout.readline()
        if not line:
            break
        line_str = line.decode('utf-8', errors='ignore').strip()
        if "%" in line_str or "ETA" in line_str:
            print(f"Aria2c: {line_str}")
            match = re.search(r'\[(.*?)\]', line_str)
            if match:
                maybe_edit_status(f"⬇️ Downloading...\n{match.group(1)}\n\nFile: {filename}")

    await process.wait()
    return dest_path if os.path.exists(dest_path) else None


def download_with_ytdlp(url):
    url = bypass_pixeldrain(url)
    ydl_opts = {
        'outtmpl': f'{FAST_DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'noplaylist': True,
        'quiet': False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


async def download_torrent(url, dest_dir):
    cmd = ["aria2c", "--seed-time=0", "--bt-stop-timeout=900", "--file-allocation=none",
           "--summary-interval=1", "-d", dest_dir, url]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    while True:
        line = await process.stdout.readline()
        if not line:
            break
        line_str = line.decode('utf-8', errors='ignore').strip()
        if "%" in line_str or "ETA" in line_str:
            print(f"Aria2c: {line_str}")
            match = re.search(r'\[(.*?)\]', line_str)
            if match:
                maybe_edit_status(f"⬇️ Downloading...\n{match.group(1)}\n\nFile: Torrent")

    await process.wait()
    return dest_dir if list_torrent_files(dest_dir) else None


# ---------------- EXTRACT ----------------
async def extract_with_7z(archive_path, extract_dir):
    edit_status("📦 Extracting archive safely...\n(1-2 minute le sakta hai, please wait)")
    cmd = f'7z x "{archive_path}" -o"{extract_dir}" -y'
    process = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        line_str = line.decode('utf-8', errors='ignore').strip()
        if "Extracting" in line_str:
            print(line_str)
    await process.wait()
    return extract_dir


# ---------------- UPLOAD PROGRESS (Pyrogram callback) ----------------
def upload_progress(current, total):
    pct = current * 100 / total if total else 0
    maybe_edit_status(f"🚀 Uploading... {pct:.1f}%\n{current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB")


# ---------------- MAIN ----------------
async def main():
    app = Client(
        "leech_worker_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        max_concurrent_transmissions=6,
    )
    await app.start()

    if TRIGGER_MSG_ID and TRIGGER_MSG_ID != "none":
        try:
            await app.delete_messages(CHAT_ID, int(TRIGGER_MSG_ID))
        except Exception:
            pass

    send_status("⚡ Starting Fast Leech on GitHub Actions...")

    try:
        url = bypass_pixeldrain(URL)
        torrent_mode = is_torrent_source(url)
        source_files = []

        if torrent_mode:
            torrent_dir = await download_torrent(url, FAST_DOWNLOAD_DIR)
            if torrent_dir:
                source_files = list_torrent_files(torrent_dir)
        else:
            filename = get_real_filename(url)
            filepath = await download_aria2(url, FAST_DOWNLOAD_DIR, filename)
            if not filepath:
                print("Aria2 failed, trying yt-dlp...")
                filepath = download_with_ytdlp(url)
            if filepath and os.path.exists(filepath):
                source_files = [filepath]

        if not source_files:
            edit_status("❌ Download failed.")
            return

        size_gb = sum(os.path.getsize(p) for p in source_files) / (1024 ** 3)
        edit_status(f"✅ Downloaded! Size: {size_gb:.2f} GB.\nProcessing...")

        files_to_send = []
        came_from_archive = False
        extract_no = 0

        for src in source_files:
            if src.lower().endswith(('.zip', '.rar', '.7z')):
                came_from_archive = True
                extract_no += 1
                extract_path = os.path.join(FAST_EXTRACT_DIR, f"{int(time.time())}_{extract_no}")
                os.makedirs(extract_path, exist_ok=True)
                await extract_with_7z(src, extract_path)
                os.remove(src)
                for root, _, files in os.walk(extract_path):
                    for file in files:
                        if file.lower().endswith(ALLOWED_EXTS):
                            files_to_send.append(os.path.join(root, file))
            elif torrent_mode:
                if src.lower().endswith(ALLOWED_EXTS):
                    files_to_send.append(src)
            else:
                files_to_send.append(src)

        files_to_send.sort(key=natural_key)
        total_files = len(files_to_send)

        if total_files == 0:
            edit_status("❌ No supported files found to upload.")
            return

        final_names, guessed = build_final_names(files_to_send, came_from_archive)
        if guessed:
            _tg_call("sendMessage", chat_id=CHAT_ID,
                     text="⚠️ Episode detect nahi hua, order se number diya:\n" + "\n".join(guessed[:15]))

        thumb_path = prepare_thumb()

        for idx, f in enumerate(files_to_send, 1):
            f = apply_name(f, final_names.get(f))
            f_name = os.path.basename(f)
            f_ext = os.path.splitext(f)[1].lower()
            f_size = os.path.getsize(f) / (1024 ** 3)

            if f_size > 2.0:
                _tg_call("sendMessage", chat_id=CHAT_ID, text=f"⚠️ {f_name} 2GB se bada hai ({f_size:.2f}GB), skip kar raha hoon.")
                os.remove(f)
                continue

            edit_status(f"⬆️ Uploading {idx}/{total_files}:\n{f_name}")
            caption = f_name
            extra = {"thumb": thumb_path} if thumb_path else {}

            try:
                if f_ext in VIDEO_EXTS and UPLOAD_FORMAT != "document":
                    await app.send_video(CHAT_ID, f, caption=caption, supports_streaming=True,
                                          progress=upload_progress, **probe_video(f), **extra)
                else:
                    await app.send_document(CHAT_ID, f, caption=caption,
                                             progress=upload_progress, **extra)
            except Exception as up_err:
                print(f"Error uploading {f_name}: {up_err}")
                _tg_call("sendMessage", chat_id=CHAT_ID, text=f"❌ Failed to upload {f_name}\nError: {up_err}")

            os.remove(f)

        edit_status(f"🏁 Successfully Leeched & Uploaded {total_files} file(s)!")

    except Exception as e:
        edit_status(f"❌ Error: {e}")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
