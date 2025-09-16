import os
import shutil
import psutil
import asyncio
from time import time
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import PeerIdInvalid, BadRequest
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from helpers.utils import processMediaGroup, progressArgs, send_media
from helpers.files import get_download_path, fileSizeLimit, get_readable_file_size, get_readable_time, cleanup_download
from helpers.msg import getChatMsgID, get_file_name, get_parsed_msg
from config import PyroConf
from logger import LOGGER

# Initialize clients
bot = Client(
    "media_bot",
    api_id=PyroConf.API_ID,
    api_hash=PyroConf.API_HASH,
    bot_token=PyroConf.BOT_TOKEN,
    workers=50,  # Reduced from 100 to balance performance and resource usage
    parse_mode=ParseMode.MARKDOWN,
)
user = Client("user_session", session_string=PyroConf.SESSION_STRING, workers=50)

RUNNING_TASKS = set()

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    task.add_done_callback(lambda _: RUNNING_TASKS.discard(task))
    return task

async def progress_callback(current: int, total: int, message: Message, start_time: float, action: str = "Downloading"):
    """Custom progress callback for file downloads."""
    elapsed = time() - start_time
    if elapsed >= 10:  # Update every 10 seconds to reduce API calls
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

async def download_media_from_url(bot: Client, message: Message, post_url: str):
    """Handle downloading media from a Telegram post URL."""
    post_url = post_url.split("?", 1)[0]  # Clean URL
    try:
        chat_id, message_id = getChatMsgID(post_url)
        chat_message = await user.get_messages(chat_id=chat_id, message_ids=message_id)
        LOGGER(__name__).info(f"Downloading media from URL: {post_url}")

        # Check file size limit
        file_size = 0
        if chat_message.document:
            file_size = chat_message.document.file_size
        elif chat_message.video:
            file_size = chat_message.video.file_size
        elif chat_message.audio:
            file_size = chat_message.audio.file_size

        if file_size and not await fileSizeLimit(file_size, message, "download", user.me.is_premium):
            return

        # Parse captions and text
        parsed_caption = await get_parsed_msg(chat_message.caption or "", chat_message.caption_entities)
        parsed_text = await get_parsed_msg(chat_message.text or "", chat_message.entities)

        # Handle media groups
        if chat_message.media_group_id:
            if not await processMediaGroup(chat_message, bot, message):
                await message.reply("**Could not extract valid media from the group.**")
            return

        # Handle single media
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
            await send_media(
                bot, message, media_path, media_type, parsed_caption, progress_message, start_time
            )
            cleanup_download(media_path)
            await progress_message.delete()

        # Handle text-only messages
        elif chat_message.text or chat_message.caption:
            await message.reply(parsed_text or parsed_caption)
        else:
            await message.reply("**No media or text found in the post.**")

    except (PeerIdInvalid, BadRequest):
        await message.reply("**Ensure the user session has access to the chat.**")
    except Exception as e:
        LOGGER(__name__).error(f"Error downloading {post_url}: {e}")
        await message.reply(f"**❌ Error: {str(e)}**")

@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    """Handle /start command."""
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
    """Handle /help command."""
    help_text = (
        "💡 **Media Downloader Bot Help**\n\n"
        "➤ **Download Media**\n"
        "   - Use `/dl <post_URL>` or paste a Telegram post link.\n\n"
        "➤ **Batch Download**\n"
        "   - Use `/bdl <start_link> <end_link>` to download a range of posts.\n"
        "     Example: `/bdl https://t.me/channel/100 https://t.me/channel/120`\n\n"
        "➤ **Requirements**\n"
        "   - User session must have access to the channel.\n\n"
        "➤ **Commands**\n"
        "   - `/killall`: Cancel all running downloads.\n"
        "   - `/logs`: Download bot logs.\n"
        "   - `/stats`: View bot status.\n\n"
        "Example: `/dl https://t.me/itsSmartDev/547`"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Updates", url="https://t.me/itsSmartDev")]])
    await message.reply(help_text, reply_markup=markup, disable_web_page_preview=True)

@bot.on_message(filters.command("dl") & filters.private)
async def download_media_command(bot: Client, message: Message):
    """Handle /dl command."""
    if len(message.command) < 2:
        await message.reply("**Provide a post URL after /dl.**")
        return
    await track_task(download_media_from_url(bot, message, message.command[1]))

@bot.on_message(filters.command("bdl") & filters.private)
async def batch_download(bot: Client, message: Message):
    """Handle /bdl command for batch downloads."""
    args = message.text.split()
    if len(args) != 3 or not all(arg.startswith("https://t.me/") for arg in args[1:]):
        await message.reply(
            "🚀 **Batch Download**\n"
            "Use: `/bdl <start_link> <end_link>`\n"
            "Example: `/bdl https://t.me/channel/100 https://t.me/channel/120`"
        )
        return

    try:
        start_chat, start_id = getChatMsgID(args[1])
        end_chat, end_id = getChatMsgID(args[2])
        if start_chat != end_chat:
            await message.reply("**Links must be from the same channel.**")
            return
        if start_id > end_id:
            await message.reply("**Start ID cannot exceed end ID.**")
            return

        prefix = args[1].rsplit("/", 1)[0]
        loading = await message.reply(f"📥 **Downloading posts {start_id}–{end_id}…**")

        downloaded, skipped, failed = 0, 0, 0
        # Process downloads in batches of 10 to balance speed and API limits
        batch_size = 10
        for i in range(start_id, end_id + 1, batch_size):
            batch_urls = [f"{prefix}/{msg_id}" for msg_id in range(i, min(i + batch_size, end_id + 1))]
            tasks = []
            for url in batch_urls:
                try:
                    chat_id, msg_id = getChatMsgID(url)
                    chat_msg = await user.get_messages(chat_id=chat_id, message_ids=msg_id)
                    if not chat_msg or not (chat_msg.media_group_id or chat_msg.media or chat_msg.text or chat_msg.caption):
                        skipped += 1
                        continue
                    tasks.append(track_task(download_media_from_url(bot, message, url)))
                except Exception as e:
                    failed += 1
                    LOGGER(__name__).error(f"Error at {url}: {e}")

            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    downloaded += len(tasks)
                except asyncio.CancelledError:
                    await loading.delete()
                    await message.reply(f"**❌ Batch canceled after {downloaded} posts.**")
                    return
                except Exception as e:
                    failed += len(tasks)
                    LOGGER(__name__).error(f"Batch error: {e}")

        await loading.delete()
        await message.reply(
            "**✅ Batch Complete!**\n"
            f"📥 Downloaded: `{downloaded}`\n"
            f"⏭️ Skipped: `{skipped}`\n"
            f"❌ Failed: `{failed}`"
        )
    except Exception as e:
        await message.reply(f"**❌ Error: {str(e)}**")

@bot.on_message(filters.private & ~filters.command(["start", "help", "dl", "bdl", "stats", "logs", "killall"]))
async def handle_message(bot: Client, message: Message):
    """Handle direct URL messages."""
    if message.text and not message.text.startswith("/"):
        await track_task(download_media_from_url(bot, message, message.text))

@bot.on_message(filters.command("stats") & filters.private)
async def stats(_, message: Message):
    """Handle /stats command."""
    uptime = get_readable_time(time() - PyroConf.BOT_START_TIME)
    total, used, free = shutil.disk_usage(".")
    stats = (
        f"**Bot Status**\n\n"
        f"➜ Uptime: `{uptime}`\n"
        f"➜ Disk: `{get_readable_file_size(total)}` total, `{get_readable_file_size(used)}` used, `{get_readable_file_size(free)}` free\n"
        f"➜ Memory: `{psutil.virtual_memory().percent}%`\n"
        f"➜ CPU: `{psutil.cpu_percent(interval=0.5)}%`\n"
        f"➜ Network: ↑ `{get_readable_file_size(psutil.net_io_counters().bytes_sent)}` ↓ `{get_readable_file_size(psutil.net_io_counters().bytes_recv)}`"
    )
    await message.reply(stats)

@bot.on_message(filters.command("logs") & filters.private)
async def logs(_, message: Message):
    """Handle /logs command."""
    if os.path.exists("logs.txt"):
        await message.reply_document("logs.txt", caption="**Bot Logs**")
    else:
        await message.reply("**No logs available.**")

@bot.on_message(filters.command("killall") & filters.private)
async def cancel_all_tasks(_, message: Message):
    """Handle /killall command."""
    cancelled = sum(1 for task in RUNNING_TASKS if not task.done() and task.cancel())
    await message.reply(f"**Cancelled {cancelled} task(s).**")

if __name__ == "__main__":
    try:
        LOGGER(__name__).info("Starting bot...")
        user.start()
        bot.run()
    except Exception as e:
        LOGGER(__name__).error(f"Bot crashed: {e}")
    finally:
        LOGGER(__name__).info("Bot stopped.")

