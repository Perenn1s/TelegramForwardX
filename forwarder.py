import logging
import os
import asyncio
import tweepy
from telethon import events

# --- Configuration Import ---
# Importing pre-configured client and initialization functions from the config file.
from load_Api_x import (
    client,                  # Configured Telegram Client (with proxy/session)
    Telegram_CHANNEL_USERNAME,  # Target Channel Username
    init_twitter             # Function to initialize Twitter API
)

# --- Logging Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Suppress verbose logs from Telethon
logging.getLogger('telethon').setLevel(logging.WARNING)
# Suppress handshake logs from python-socks (if proxy is enabled)
logging.getLogger('python_socks').setLevel(logging.WARNING)

# --- Global Initialization ---
logging.info("Initializing Twitter API...")
api_v1, client_v2 = init_twitter()

# Verify initialization
if not api_v1 or not client_v2:
    logging.error("❌ Twitter API initialization failed. Please check credentials or proxy settings in load_Api_x.py.")
    exit(1)
else:
    logging.info("✅ Twitter API loaded successfully.")


# --- Core Function: Post to Twitter ---
async def post_to_twitter(text, media_path=None, is_video=False):
    try:
        media_id = None

        # 1. Upload Media (if exists)
        if media_path:
            logging.info(f"Uploading media (Video={is_video}): {media_path} ...")
            try:
                if is_video:
                    # Videos must be uploaded using chunked=True
                    media = api_v1.media_upload(
                        filename=media_path,
                        chunked=True,
                        media_category='tweet_video'
                    )
                else:
                    # Images can be uploaded directly
                    media = api_v1.media_upload(filename=media_path)

                media_id = media.media_id
                logging.info(f"Media uploaded successfully. ID: {media_id}")
            except Exception as upload_error:
                logging.error(f"⚠️ Media upload failed: {upload_error}")
                # Decide here if you want to abort or continue posting text only
                # For now, we continue trying to post text.

        # 2. Prepare Content
        clean_text = text if text else ""

        # 3. Create Tweet
        if media_id:
            response = client_v2.create_tweet(text=clean_text, media_ids=[media_id])
        elif clean_text:
            response = client_v2.create_tweet(text=clean_text)
        else:
            logging.warning("⚠️ Message is empty, skipping.")
            return

        logging.info(f"🚀 Tweet sent successfully! ID: {response.data['id']}")

    except tweepy.errors.Forbidden as e:
        logging.error(f"❌ Permission Error (403): Check if Access Token has 'Write' permissions.\nDetails: {e}")
    except Exception as e:
        logging.error(f"❌ Unknown Error while tweeting: {e}")


# --- Event Listener ---
@client.on(events.NewMessage(chats=Telegram_CHANNEL_USERNAME))
async def new_message_handler(event):
    message_text = event.message.message or ""

    # Log the first 20 characters for preview
    log_text = message_text[:20].replace('\n', ' ')
    logging.info(f"📩 New message received: {log_text}...")

    media_path = None
    is_video = False

    try:
        # A. Detect Video
        if event.message.video:
            logging.info("🎥 Video detected, downloading...")
            is_video = True
            media_path = await event.download_media()
            logging.info(f"Video download complete: {media_path}")

        # B. Detect Photo (Only if not video)
        elif event.message.photo:
            logging.info("📸 Photo detected, downloading...")
            media_path = await event.download_media()
            logging.info(f"Photo download complete: {media_path}")

        # C. Execute Forwarding
        if media_path or message_text:
            await post_to_twitter(message_text, media_path=media_path, is_video=is_video)
        else:
            logging.info("Ignoring unsupported message type.")

    except Exception as e:
        logging.error(f"Error handling message: {e}")

    finally:
        # D. Cleanup Local Files
        if media_path and os.path.exists(media_path):
            try:
                os.remove(media_path)
                logging.info(f"🗑️ Cleaned up file: {media_path}")
            except Exception as e:
                logging.error(f"Failed to delete file: {e}")


# --- Main Loop ---
async def main():
    print(f"🚀 Bot started! Listening to: {Telegram_CHANNEL_USERNAME}")
    print("Tip: Check load_Api_x.py for proxy settings.")

    # Start Telegram Client
    # session and proxy are already configured in load_Api_x
    await client.start()
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped.")
