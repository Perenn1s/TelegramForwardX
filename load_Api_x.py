import logging
import asyncio
import os
import sys
import configparser
import socks
import tweepy
from telethon import TelegramClient, events

# ==========================================
#        1. Load External Configuration
# ==========================================

# Determine the base directory (Compatible with compiled .exe paths)
if getattr(sys, 'frozen', False):
    # If bundled as an exe
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # If running as a standard python script
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config_path = os.path.join(BASE_DIR, 'config.ini')

# Check if config file exists
if not os.path.exists(config_path):
    print(f"❌ Error: Configuration file not found: {config_path}")
    print("Please create a 'config.ini' file and fill in your API credentials.")
    input("Press Enter to exit...")
    sys.exit(1)

# Read Configuration
config = configparser.ConfigParser(interpolation=None)
config.read(config_path, encoding='utf-8')

try:
    # --- Read Settings ---
    ENABLE_PROXY = config.getboolean('Settings', 'enable_proxy')
    PROXY_HOST = config.get('Settings', 'proxy_host')
    PROXY_PORT = config.getint('Settings', 'proxy_port')

    # --- Read Twitter Credentials ---
    API_KEY = config.get('Twitter', 'API_KEY')
    API_SECRET = config.get('Twitter', 'API_SECRET')
    ACCESS_TOKEN = config.get('Twitter', 'ACCESS_TOKEN')
    ACCESS_SECRET = config.get('Twitter', 'ACCESS_SECRET')
    BEARER_TOKEN = config.get('Twitter', 'BEARER_TOKEN')

    # --- Read Telegram Credentials ---
    Telegram_API_ID = config.getint('Telegram', 'Telegram_API_ID')
    Telegram_API_HASH = config.get('Telegram', 'Telegram_API_HASH')
    Telegram_CHANNEL_USERNAME = config.get('Telegram', 'Telegram_CHANNEL_USERNAME')

except Exception as e:
    print(f"❌ Configuration format error: {e}")
    print("Please check if 'config.ini' is missing any required fields.")
    input("Press Enter to exit...")
    sys.exit(1)

# ==========================================
#           2. Initialization Logic
# ==========================================

# --- A. Configure Twitter Proxy (via Environment Variables) ---
# Tweepy v2 does not support the 'proxy' argument directly; we must use os.environ.
if ENABLE_PROXY:
    proxy_str = f"http://{PROXY_HOST}:{PROXY_PORT}"
    os.environ['HTTP_PROXY'] = proxy_str
    os.environ['HTTPS_PROXY'] = proxy_str
    print(f"🌍 Twitter Proxy Environment Variable Set: {proxy_str}")
else:
    # Ensure environment variables are cleared if proxy is disabled
    os.environ.pop('HTTP_PROXY', None)
    os.environ.pop('HTTPS_PROXY', None)

# --- B. Initialize Twitter Object ---
def init_twitter():
    try:
        # 1. V1.1 (For Media Uploads)
        auth = tweepy.OAuthHandler(API_KEY, API_SECRET)
        auth.set_access_token(ACCESS_TOKEN, ACCESS_SECRET)
        api_v1 = tweepy.API(auth)

        # 2. V2 (For Posting Tweets)
        # Note: Proxy is handled automatically via os.environ set above
        client_v2 = tweepy.Client(
            bearer_token=BEARER_TOKEN,
            consumer_key=API_KEY,
            consumer_secret=API_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_SECRET
        )
        return api_v1, client_v2
    except Exception as e:
        logging.error(f"Twitter Initialization Failed: {e}")
        return None, None


# --- C. Initialize Telegram Client ---
# 1. Calculate Session Path
script_dir = os.path.dirname(os.path.abspath(__file__))
session_path = os.path.join(script_dir, 'meme_bot_session')

# 2. Configure Telegram Proxy
tg_proxy_config = None
if ENABLE_PROXY:
    # Telethon requires a tuple: (Type, Host, Port)
    tg_proxy_config = (socks.SOCKS5, PROXY_HOST, PROXY_PORT)
    print(f"🌍 Telegram Proxy Configured: {tg_proxy_config}")

# 3. Create Client (Pass proxy config)
client = TelegramClient(
    session_path,
    Telegram_API_ID,
    Telegram_API_HASH,
    proxy=tg_proxy_config
)


# --- 3. Message Listener Placeholder ---
# This serves as the base decorator source for the main script
@client.on(events.NewMessage(chats=Telegram_CHANNEL_USERNAME))
async def handler(event):
    pass


# --- D. Main Execution (Connection Test) ---
async def main():
    print("--- Starting Connection Test ---")

    # 1. Check Twitter
    api_v1, client_v2 = init_twitter()
    if not api_v1 or not client_v2:
        print("❌ Twitter login failed. Exiting.")
        return

    # Verify Twitter Identity
    try:
        me_twitter = client_v2.get_me()
        print(f"✅ Twitter Connected Successfully: @{me_twitter.data.username}")
    except Exception as e:
        print(f"❌ Twitter Connection Failed: {e}")
        print("💡 Tip: If proxy is enabled, check your port (e.g., Clash=7890, v2ray=10808)")
        return

    # 2. Check Telegram
    print("🚀 Connecting to Telegram...")

    # Smart Start: Handles login, 2FA, and reconnection
    await client.start(
        phone=lambda: input("Please enter your phone number (e.g., +1xxxx): "),
        code_callback=lambda: input("Please enter the code you received: "),
        password=lambda: input("Please enter your 2FA password (if enabled): ")
    )

    print("✅ Telegram Login Successful!")
    me = await client.get_me()
    print(f"👋 User: {me.first_name} (ID: {me.id})")
    print("Note: This script is for Session generation/testing. Run 'main.py' to start the service.")

if __name__ == '__main__':
    # --- Configure Logging ---
    logging.basicConfig(
        format='%(asctime)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Program stopped by user.")
