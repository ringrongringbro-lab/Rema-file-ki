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

from pyrogram import Client
import yt_dlp

# ---------------- ENV VARS (workflow se aate hain) ----------------
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

URL = os.getenv("URL")
META_NAME = os.getenv("META_NAME", "Unknown")
META_SEASON = os.getenv("META_SEASON", "01")
META_VOICE = os.getenv("META_VOICE", "Hindi")
META_QUALITY = os.getenv("META_QUALITY", "1080p")
MANUAL_EP = os.getenv("MANUAL_EP", "01")
CHAT_ID = int(os.getenv("CHAT_ID"))
TRIGGER_MSG_ID = os.getenv("TRIGGER_MSG_ID", "none")

FAST_DOWNLOAD_DIR = "downloads"
FAST_EXTRACT_DIR = "extracted"
os.makedirs(FAST_DOWNLOAD_DIR, exist_ok=True)
os.makedirs(FAST_EXTRACT_DIR, exist_ok=True)

VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.flv', '.ts', '.3gp')
DOC_EXTS = ('.zip', '.rar', '.7z', '.pdf')
ALLOWED_EXTS = VIDEO_EXTS + DOC_EXTS

CAPTION_TEMPLATE = """{name}
➖➖➖➖➖➖➖➖➖➖➖➖
Voice: {voice}
Season: {season}
➖➖➖➖➖➖➖➖➖➖➖➖
» Powered by ~♡@asi_anime✦
➖➖➖➖➖➖➖➖➖➖➖➖
QUALITY - {quality}
EPIS0DE ━ {episode}

» Uploaded by Bot"""


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


def build_caption(episode):
    return CAPTION_TEMPLATE.format(
        name=META_NAME or "Unknown",
        season=META_SEASON or "01",
        voice=META_VOICE or "Hindi",
        quality=META_QUALITY or "1080p",
        episode=episode,
    )


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
        filename = get_real_filename(url)

        filepath = await download_aria2(url, FAST_DOWNLOAD_DIR, filename)
        if not filepath:
            print("Aria2 failed, trying yt-dlp...")
            filepath = download_with_ytdlp(url)

        if not filepath or not os.path.exists(filepath):
            edit_status("❌ Download failed.")
            return

        size_gb = os.path.getsize(filepath) / (1024 ** 3)
        edit_status(f"✅ Downloaded! Size: {size_gb:.2f} GB.\nProcessing...")

        files_to_send = []
        is_archive = filepath.lower().endswith(('.zip', '.rar', '.7z'))

        if is_archive:
            extract_path = os.path.join(FAST_EXTRACT_DIR, str(int(time.time())))
            os.makedirs(extract_path, exist_ok=True)
            await extract_with_7z(filepath, extract_path)
            os.remove(filepath)
            for root, _, files in os.walk(extract_path):
                for file in files:
                    if file.lower().endswith(ALLOWED_EXTS):
                        files_to_send.append(os.path.join(root, file))
        else:
            files_to_send = [filepath]

        files_to_send.sort()
        total_files = len(files_to_send)

        if total_files == 0:
            edit_status("❌ No supported files found to upload.")
            return

        for idx, f in enumerate(files_to_send, 1):
            f_name = os.path.basename(f)
            f_ext = os.path.splitext(f)[1].lower()
            f_size = os.path.getsize(f) / (1024 ** 3)

            if f_size > 2.0:
                _tg_call("sendMessage", chat_id=CHAT_ID, text=f"⚠️ {f_name} 2GB se bada hai ({f_size:.2f}GB), skip kar raha hoon.")
                os.remove(f)
                continue

            episode = f"{idx:02d}" if total_files > 1 else (MANUAL_EP or "01")
            edit_status(f"⬆️ Uploading {idx}/{total_files}:\n{f_name}")
            caption = build_caption(episode)

            try:
                if f_ext in VIDEO_EXTS:
                    await app.send_video(CHAT_ID, f, caption=caption, supports_streaming=True,
                                          progress=upload_progress)
                else:
                    await app.send_document(CHAT_ID, f, caption=caption,
                                             progress=upload_progress)
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
