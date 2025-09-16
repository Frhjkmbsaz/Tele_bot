import os
import shutil
import psutil
import asyncio
from time import time
from threading import Thread
from flask import Flask

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import PeerIdInvalid, BadRequest
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from helpers.utils import processMediaGroup, progressArgs, send_media
from helpers.files import get_download_path, fileSizeLimit, get_readable_file_size, get_readable_time, cleanup_download
from helpers.msg import getChatMsgID, get_file_name, get_parsed_msg
from config import PyroConf
from logger import LOGGER

# ====== Flask dummy server ======
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

# Start Flask server in separate thread
flask_thread = Thread(target=run_flask)
flask_thread.start()

# ====== Telegram bot clients ======
bot = Client(
    "media_bot",
    api_id=PyroConf.API_ID,
    api_hash=PyroConf.API_HASH,
    bot_token=PyroConf.BOT_TOKEN,
    workers=50,
    parse_mode=ParseMode.MARKDOWN,
)

user = Client(
    "user_session",
    session_string=PyroConf.SESSION_STRING,
    workers=50
)

RUNNING_TASKS = set()

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    task.add_done_callback(lambda _: RUNNING_TASKS.discard(task))
    return task

async def progress_callback(current: int, total: int, message: Message, start_time: float, action: str = "Downloading"):
    elapsed = time() - start_time
    if elapsed >= 10:
        percent = (current / total) * 100
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        await message.edit_text(
            f"**{action}...**\n"
            f"Progress: {percent:.1f}%\n"
            f"Downloaded: {get_readable_file_size(current)} / {get_readable_file_size(total)}\n"
            f"Speed: {get_readable_file_size(speed)}/s\n"
            f"ETA: {get_readable_time(eta)}"
        )

# ====== Media download logic ======
async def download_media_from_url(bot: Client, message: Message, post_url: str):
    post_url = post_url.split("?", 1)[0]
    try:
        chat_id, message_id = getChatMsgID(post_url)
        chat_message = await user.get_messages(chat_id=chat_id, message_ids=message_id)
        LOGGER(__name__).info(f"Downloading media from URL: {post_url}")

        # File size check
        file_size = 0
        if chat_message.document:
            file_size = chat_message.document.file_size
        elif chat_message.video:
            file_size = chat_message.video.file_size
        elif chat_message.audio:
            file_size = chat_message.audio.file_size

        if file_size and not await fileSizeLimit(file_size, message, "download", user.me.is_premium):
            return

        parsed_caption = await get_parsed_msg(chat_message.caption or "", chat_message.caption_entities)
        parsed_text = await get_parsed_msg(chat_message.text or "", chat_message.entities)

        if chat_message.media_group_id:
            if not await processMediaGroup(chat_message, bot, message):
                await message.reply("**Could not extract valid media from the group.**")
            return

        if chat_message.media:
            start_time = time()
            progress_message = await message.reply("**📥 Downloading...**")
            filename = get_file_name(message_id, chat_message)
            download_path = get_download_path(message.id, filename)

            media_path = await chat_message.download(
                file_name=download_path,
                progress=progress_callback,
                progress_args=(progress_message, start_time, "Downloading"),
            )
            LOGGER(__name__).info(f"Downloaded media: {media_path}")

            media_type = (
                "photo" if chat_message.photo else
                "video" if chat_message.video else
                "audio" if chat_message.audio else
                "document"
            )
            await send_media(bot, message, media_path, media_type, parsed_caption, progress_message, start_time)
            cleanup_download(media_path)
            await progress_message.delete()
        elif chat_message.text or chat_message.caption:
            await message.reply(parsed_text or parsed_caption)
        else:
            await message.reply("**No media or text found in the post.**")
    except (PeerIdInvalid, BadRequest):
        await message.reply("**Ensure the user session has access to the chat.**")
    except Exception as e:
        LOGGER(__name__).error(f"Error downloading {post_url}: {e}")
        await message.reply(f"**❌ Error: {str(e)}**")

# ====== Bot commands ======
@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    welcome_text = (
        "👋 **Welcome to Media Downloader Bot!**\n\n"
        "I can download media from Telegram posts, including restricted channels.\n"
        "Send a link directly or use `/dl <link>`.\n"
        "Use `/bdl` for batch downloads.\n\n"
        "ℹ️ Use `/help` for more details.\n"
        "🔒 Ensure the user session has access to the channel."
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Updates", url="https://t.me/itsSmartDev")]])
    await message.reply(welcome_text, reply_markup=markup, disable_web_page_preview=True)

@bot.on_message(filters.command("help") & filters.private)
async def help_command(_, message: Message):
    help_text = (
        "💡 **Media Downloader Bot Help**\n\n"
        "➤ /dl <post_URL> - Download single post\n"
        "➤ /bdl <start_link> <end_link> - Batch download\n"
        "➤ /stats - View bot stats\n"
        "➤ /logs - Get bot logs\n"
        "➤ /killall - Cancel all running downloads\n"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Updates", url="https://t.me/itsSmartDev")]])
    await message.reply(help_text, reply_markup=markup, disable_web_page_preview=True)

@bot.on_message(filters.command("dl") & filters.private)
async def download_media_command(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("**Provide a post URL after /dl.**")
        return
    await track_task(download_media_from_url(bot, message, message.command[1]))

# ====== Additional commands: batch download, stats, logs, killall ======
# (Use your existing code as-is)

# ====== Main ======
if __name__ == "__main__":
    try:
        LOGGER(__name__).info("Starting bot...")
        user.start()
        bot.run()
    except Exception as e:
        LOGGER(__name__).error(f"Bot crashed: {e}")
    finally:
        LOGGER(__name__).info("Bot stopped.")
