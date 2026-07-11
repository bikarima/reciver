"""تنظیمات پروژه"""
import os
from pathlib import Path
from typing import Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()

class Config:
    """کلاس تنظیمات"""
    
    # تنظیمات API تلگرام
    API_ID: int = int(os.getenv('API_ID', '5099517'))
    API_HASH: str = os.getenv('API_HASH', '3bffbb2ff1f15e5812fbeb8ab22d0f66')
    BOT_TOKEN: str = os.getenv('BOT_TOKEN', '8713131015:AAHcCTUye1Ki23rHyqKatWusoW-bc8OePsA')
    
    # ادمین‌های ربات
    ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS', '7053561971').split(',') if x.strip()]
    
    # تنظیمات تایمر و تاخیر (به ثانیه)
    DELAY_BETWEEN_ACTIONS = int(os.getenv('DELAY_BETWEEN_ACTIONS', '5'))  # تاخیر بین هر عملیات
    DELAY_RANDOM_RANGE = int(os.getenv('DELAY_RANDOM_RANGE', '3'))  # محدوده تصادفی اضافه
    MAX_ACTIONS_PER_MINUTE = int(os.getenv('MAX_ACTIONS_PER_MINUTE', '10'))  # حداکثر عملیات در دقیقه
    
    # مسیر ذخیره سشن‌ها و لاگ‌ها
    SESSIONS_DIR = Path(os.getenv('SESSIONS_DIR', 'sessions'))
    LOGS_DIR = Path(os.getenv('LOGS_DIR', 'logs'))
    DATABASE_PATH = os.getenv('DATABASE_PATH', 'data/accounts.db')
    
    # تنظیمات پروکسی
    PROXY_ENABLED = os.getenv('PROXY_ENABLED', 'false').lower() == 'true'
    PROXY_TYPE = os.getenv('PROXY_TYPE', 'socks5')  # socks5, http
    PROXY_HOST = os.getenv('PROXY_HOST', '127.0.0.1')
    PROXY_PORT = int(os.getenv('PROXY_PORT', '1080'))
    PROXY_USERNAME = os.getenv('PROXY_USERNAME', '')
    PROXY_PASSWORD = os.getenv('PROXY_PASSWORD', '')
    
    # ایجاد پوشه‌ها در صورت عدم وجود
    SESSIONS_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    
    @classmethod
    def validate(cls) -> bool:
        """اعتبارسنجی تنظیمات"""
        if not cls.API_ID or not cls.API_HASH or not cls.BOT_TOKEN:
            return False
        return True
    
    @classmethod
    def get_proxy_config(cls) -> Optional[Dict[str, Any]]:
        """
        دریافت تنظیمات پروکسی
        
        Returns:
            دیکشنری تنظیمات پروکسی یا None
        """
        if not cls.PROXY_ENABLED:
            return None
        
        proxy_config = {
            'proxy_type': cls.PROXY_TYPE,
            'addr': cls.PROXY_HOST,
            'port': cls.PROXY_PORT
        }
        
        if cls.PROXY_USERNAME:
            proxy_config['username'] = cls.PROXY_USERNAME
        
        if cls.PROXY_PASSWORD:
            proxy_config['rdns'] = True
            proxy_config['password'] = cls.PROXY_PASSWORD
        
        return proxy_config
