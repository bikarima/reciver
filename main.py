
import asyncio
import os
import sys
from pathlib import Path

# همیشه CWD رو به پوشه main.py ست کن
os.chdir(Path(__file__).parent)

from src.config import Config
from src.bot import BotHandler
from src.utils import setup_logger

logger = setup_logger(__name__, 'logs/app.log')

async def main():
    """تابع اصلی"""
    # اعتبارسنجی تنظیمات
    if not Config.validate():
        logger.error("تنظیمات نامعتبر است. لطفاً فایل .env را بررسی کنید.")
        return
    
    logger.info("شروع برنامه...")
    
    # راه‌اندازی ربات
    bot_handler = BotHandler()
    await bot_handler.start()

if __name__ == '__main__':
    try:
        
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("برنامه متوقف شد.")

