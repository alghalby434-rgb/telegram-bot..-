import os
import asyncio
import logging
from flask import Flask
from threading import Thread
from telegram import LinkPreviewOptions, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

app = Flask(__name__)

@app.route('/health')
def health_check():
    return {"status": "healthy"}, 200

def run_flask():
    # قراءة المنفذ تلقائياً من بيئة Render مع وضع 10000 أو 8080 كاحتياطي لتفادي أي خطأ
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)
FACEBOOK_DOWNLOAD_ERROR = (
    "عذراً، لم أتمكن من تحميل فيديو فيسبوك هذا، يرجى التأكد من أن "
    "الحساب أو الفيديو عام (Public)."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً بك! أرسل لي أي رابط فيديو وسأقوم بتنزيله لك فوراً.",
        link_preview_options=NO_LINK_PREVIEW,
    )

class VideoDownloadError(RuntimeError):
    """Raised when yt-dlp cannot extract or download a video."""


def _metadata_int(info: dict, key: str) -> int | None:
    value = info.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def download_video_sync(
    url: str,
    output_path: str,
    user_id: int,
) -> dict[str, str | int | None]:
    os.makedirs("downloads", exist_ok=True)
    existing_files = set(os.listdir("downloads"))
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'max_filesize': 50 * 1024 * 1024,
        'noplaylist': True,
        'merge_output_format': 'mp4',
        'writethumbnail': True,
        'postprocessors': [
            {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'},
        ],
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'nocheckcertificate': True,
        'socket_timeout': 10,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'referer': 'https://www.facebook.com/',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise VideoDownloadError("yt-dlp returned no video metadata")

            download_result = ydl.download([url])
            if download_result not in (None, 0):
                raise VideoDownloadError(
                    f"yt-dlp returned download status {download_result}"
                )
    except Exception as exc:
        if isinstance(exc, VideoDownloadError):
            raise
        raise VideoDownloadError(str(exc)) from exc

    created_files = [
        os.path.join("downloads", file_name)
        for file_name in os.listdir("downloads")
        if file_name not in existing_files
        and str(user_id) in file_name
        and os.path.isfile(os.path.join("downloads", file_name))
    ]
    video_extensions = {'.mp4', '.m4v', '.mkv', '.mov', '.webm', '.avi'}
    video_files = [
        file_path
        for file_path in created_files
        if os.path.splitext(file_path)[1].lower() in video_extensions
    ]
    if not video_files:
        raise VideoDownloadError("yt-dlp did not produce a video file")

    thumbnail_extensions = {'.jpg', '.jpeg', '.png', '.webp'}
    thumbnail_files = [
        file_path
        for file_path in created_files
        if os.path.splitext(file_path)[1].lower() in thumbnail_extensions
    ]
    info_metadata = {
        'duration': _metadata_int(info, 'duration'),
        'width': _metadata_int(info, 'width'),
        'height': _metadata_int(info, 'height'),
        'thumbnail_path': (
            max(thumbnail_files, key=os.path.getmtime)
            if thumbnail_files
            else None
        ),
    }
    return {
        'file_path': max(video_files, key=os.path.getmtime),
        **info_metadata,
    }

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text(
            "يرجى إرسال رابط صحيح يبدأ بـ http أو https.",
            link_preview_options=NO_LINK_PREVIEW,
        )
        return

    status_msg = await update.message.reply_text(
        "⚡ جاري تنزيل الفيديو تلقائياً...",
        link_preview_options=NO_LINK_PREVIEW,
    )

    output_template = f"downloads/{update.effective_user.id}_%(id)s.%(ext)s"
    try:
        download_result = await asyncio.to_thread(
            download_video_sync,
            url,
            output_template,
            update.effective_user.id,
        )
    except Exception as exc:
        logging.error("Download Error: %s", exc)
        await status_msg.edit_text(
            FACEBOOK_DOWNLOAD_ERROR,
            link_preview_options=NO_LINK_PREVIEW,
        )
        return

    file_path = download_result['file_path']
    thumbnail_path = download_result['thumbnail_path']
    video_metadata = {
        key: download_result[key]
        for key in ('duration', 'width', 'height')
        if download_result[key] is not None
    }

    try:
        await status_msg.edit_text(
            "📤 جاري رفع الفيديو إلى تلجرام...",
            link_preview_options=NO_LINK_PREVIEW,
        )

        with open(file_path, 'rb') as video_file:
            if thumbnail_path:
                with open(thumbnail_path, 'rb') as thumbnail_file:
                    await context.bot.send_video(
                        chat_id=update.effective_chat.id,
                        video=video_file,
                        thumbnail=thumbnail_file,
                        caption="تم التحميل بنجاح! ✨",
                        supports_streaming=True,
                        **video_metadata,
                    )
            else:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=video_file,
                    caption="تم التحميل بنجاح! ✨",
                    supports_streaming=True,
                    **video_metadata,
                )

        os.remove(file_path)
        if thumbnail_path and os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)
        await status_msg.delete()
    except Exception as exc:
        logging.error("Upload Error: %s", exc)
        if os.path.exists(file_path):
            os.remove(file_path)
        if thumbnail_path and os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)
        await status_msg.edit_text(
            FACEBOOK_DOWNLOAD_ERROR,
            link_preview_options=NO_LINK_PREVIEW,
        )

def main():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("البوت يعمل الآن بنجاح...")
    application.run_polling()

if __name__ == "__main__":
    main()
