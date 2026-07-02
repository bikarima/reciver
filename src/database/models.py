"""مدل‌های دیتابیس"""
import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

@dataclass
class User:
    """مدل کاربر"""
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    created_at: Optional[str] = None
    is_admin: bool = False
    is_approved: bool = False  # آیا سازنده بهش دسترسی داده
    referred_by: Optional[int] = None
    referral_count: int = 0

@dataclass
class Account:
    """مدل اکانت"""
    id: Optional[int] = None
    user_id: int = None
    phone: str = None
    telegram_user_id: Optional[int] = None
    telegram_username: Optional[str] = None
    session_path: Optional[str] = None
    created_at: Optional[str] = None
    status: str = "active"  # active, inactive, banned
    added_by: Optional[int] = None  # کسی که این اکانت رو اضافه کرده
    country_code: Optional[str] = None  # کد کشور (مثلاً IR, US, GB)
    password: Optional[str] = None  # پسورد اکانت (اگر تغییر داده شده باشد)

class Database:
    """کلاس مدیریت دیتابیس"""
    
    def __init__(self, db_path: str = "data/accounts.db"):
        """مقداردهی اولیه"""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
    
    async def init_db(self):
        """ایجاد جداول دیتابیس"""
        async with aiosqlite.connect(self.db_path) as db:
            # جدول کاربران
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_admin BOOLEAN DEFAULT 0,
                    is_approved BOOLEAN DEFAULT 0,
                    referred_by INTEGER,
                    referral_count INTEGER DEFAULT 0,
                    FOREIGN KEY (referred_by) REFERENCES users (user_id)
                )
            """)
            
            # جدول اکانت‌ها
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    telegram_user_id INTEGER,
                    telegram_username TEXT,
                    session_path TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'active',
                    added_by INTEGER,
                    password TEXT,
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    FOREIGN KEY (added_by) REFERENCES users (user_id)
                )
            """)
            
            # جدول آمار
            await db.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    user_id INTEGER,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # جدول تنظیمات
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # جدول پیشرفت سناریوها
            await db.execute("""
                CREATE TABLE IF NOT EXISTS scenario_progress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    scenario_hash TEXT NOT NULL,
                    scenario_text TEXT NOT NULL,
                    last_account_index INTEGER DEFAULT 0,
                    total_accounts INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'paused',
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    UNIQUE(user_id, scenario_hash)
                )
            """)
            
            # جدول یادداشت‌های ربات
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    bot_username TEXT NOT NULL,
                    note_text TEXT NOT NULL,
                    scenario_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)
            
            # جدول تاریخچه اجرای سناریو روی رباتها
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    bot_username TEXT NOT NULL,
                    accounts_total INTEGER DEFAULT 0,
                    accounts_success INTEGER DEFAULT 0,
                    accounts_failed INTEGER DEFAULT 0,
                    scenario_text TEXT,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            # جدول پروفایل‌های لیچ شده
            await db.execute("""
                CREATE TABLE IF NOT EXISTS leeched_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_user_id INTEGER NOT NULL,
                    source_group TEXT,
                    telegram_user_id INTEGER NOT NULL,
                    access_hash INTEGER,
                    first_name TEXT,
                    last_name TEXT,
                    username TEXT,
                    bio TEXT,
                    is_used INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (owner_user_id) REFERENCES users (user_id)
                )
            """)

            # جدول عکس‌های پروفایل لیچ شده (تا 5 عکس برای هر پروفایل)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS profile_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    photo_data BLOB NOT NULL,
                    photo_index INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (profile_id) REFERENCES leeched_profiles (id) ON DELETE CASCADE
                )
            """)

            # جدول سناریوهای زمانبندی شده
            await db.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_scenarios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    scenario_text TEXT NOT NULL,
                    account_count TEXT NOT NULL,
                    workers INTEGER DEFAULT 3,
                    interval_minutes INTEGER NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    next_run_at TIMESTAMP,
                    last_run_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            await db.commit()
            
            # Migration: اضافه کردن ستون added_by اگر وجود نداره
            try:
                await db.execute("ALTER TABLE accounts ADD COLUMN added_by INTEGER")
                await db.commit()
            except:
                pass  # ستون از قبل وجود داره
            
            # Migration: اضافه کردن ستون is_approved اگر وجود نداره
            try:
                await db.execute("ALTER TABLE users ADD COLUMN is_approved BOOLEAN DEFAULT 0")
                await db.commit()
            except:
                pass  # ستون از قبل وجود داره
            
            # Migration: اضافه کردن ستون country_code اگر وجود نداره
            try:
                await db.execute("ALTER TABLE accounts ADD COLUMN country_code TEXT")
                await db.commit()
            except:
                pass  # ستون از قبل وجود داره
            
            # Migration: اضافه کردن ستون password اگر وجود نداره
            try:
                await db.execute("ALTER TABLE accounts ADD COLUMN password TEXT")
                await db.commit()
            except:
                pass  # ستون از قبل وجود داره
    
    async def add_user(self, user: User) -> bool:
        """افزودن یا بروزرسانی کاربر"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO users 
                    (user_id, username, first_name, last_name, is_admin, is_approved)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (user.user_id, user.username, user.first_name, 
                      user.last_name, user.is_admin, user.is_approved))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در افزودن کاربر: {e}")
            return False
    
    async def get_user(self, user_id: int) -> Optional[User]:
        """دریافت اطلاعات کاربر"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return User(**dict(row))
                return None
    
    async def is_admin(self, user_id: int) -> bool:
        """بررسی ادمین بودن کاربر"""
        user = await self.get_user(user_id)
        return user.is_admin if user else False
    
    async def add_account(self, account: Account) -> Optional[int]:
        """افزودن اکانت جدید"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    INSERT INTO accounts 
                    (user_id, phone, telegram_user_id, telegram_username, session_path, status, added_by, country_code, password)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (account.user_id, account.phone, account.telegram_user_id,
                      account.telegram_username, account.session_path, account.status, 
                      account.added_by, account.country_code, account.password))
                await db.commit()
                return cursor.lastrowid
        except Exception as e:
            print(f"خطا در افزودن اکانت: {e}")
            return None
    
    async def get_accounts(self, user_id: Optional[int] = None, country_code: Optional[str] = None) -> List[Account]:
        """دریافت لیست اکانت‌ها"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            
            conditions = []
            params = []
            
            if user_id:
                conditions.append("user_id = ?")
                params.append(user_id)
            
            if country_code:
                conditions.append("country_code = ?")
                params.append(country_code)
            
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            query = f"SELECT * FROM accounts WHERE {where_clause} ORDER BY created_at DESC"
            
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
                return [Account(**dict(row)) for row in rows]
    
    async def get_countries(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """دریافت لیست کشورها و تعداد اکانت‌های هر کشور"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            
            if user_id:
                query = """
                    SELECT country_code, COUNT(*) as count 
                    FROM accounts 
                    WHERE user_id = ? AND country_code IS NOT NULL AND country_code != ''
                    GROUP BY country_code 
                    ORDER BY count DESC
                """
                params = (user_id,)
            else:
                query = """
                    SELECT country_code, COUNT(*) as count 
                    FROM accounts 
                    WHERE country_code IS NOT NULL AND country_code != ''
                    GROUP BY country_code 
                    ORDER BY count DESC
                """
                params = ()
            
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    async def get_account_by_phone(self, phone: str) -> Optional[Account]:
        """دریافت اکانت با شماره تلفن"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM accounts WHERE phone = ?", (phone,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return Account(**dict(row))
                return None

    async def get_account_by_session(self, session_path: str) -> Optional[Account]:
        """دریافت اکانت با مسیر سشن"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM accounts WHERE session_path = ?", (session_path,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return Account(**dict(row))
                    return None
        except Exception as e:
            print(f"خطا در دریافت اکانت با سشن: {e}")
            return None
    
    async def update_account_status(self, account_id: int, status: str) -> bool:
        """بروزرسانی وضعیت اکانت"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE accounts SET status = ? WHERE id = ?",
                    (status, account_id)
                )
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در بروزرسانی وضعیت: {e}")
            return False
    
    async def invalidate_session(self, session_path: str) -> bool:
        """
        غیرفعال کردن سشن نامعتبر + انتقال فایل به پوشه invalid
        
        Args:
            session_path: مسیر فایل سشن
            
        Returns:
            True اگر موفق بود
        """
        import shutil
        from pathlib import Path
        
        try:
            # آپدیت وضعیت در دیتابیس
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE accounts SET status = 'inactive' WHERE session_path = ?",
                    (session_path,)
                )
                await db.commit()
            
            # انتقال فایل سشن به پوشه invalid
            session_file = Path(session_path)
            if session_file.exists():
                invalid_dir = session_file.parent / 'invalid'
                invalid_dir.mkdir(exist_ok=True)
                dest = invalid_dir / session_file.name
                # اگر فایل قبلاً اونجا بود، overwrite کن
                if dest.exists():
                    dest.unlink()
                shutil.move(str(session_file), str(dest))
                print(f"سشن نامعتبر منتقل شد: {session_file.name} → invalid/")
            
            return True
        except Exception as e:
            print(f"خطا در غیرفعال کردن سشن: {e}")
            return False
    
    async def update_account_country(self, phone: str, country_code: str) -> bool:
        """بروزرسانی کد کشور اکانت"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE accounts SET country_code = ? WHERE phone = ?",
                    (country_code, phone)
                )
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در بروزرسانی کد کشور: {e}")
            return False
    
    async def get_stats(self) -> Dict[str, Any]:
        """دریافت آمار کلی"""
        async with aiosqlite.connect(self.db_path) as db:
            # تعداد کل کاربران
            async with db.execute("SELECT COUNT(*) FROM users") as cursor:
                total_users = (await cursor.fetchone())[0]
            
            # تعداد کل اکانت‌ها
            async with db.execute("SELECT COUNT(*) FROM accounts") as cursor:
                total_accounts = (await cursor.fetchone())[0]
            
            # تعداد اکانت‌های فعال
            async with db.execute(
                "SELECT COUNT(*) FROM accounts WHERE status = 'active'"
            ) as cursor:
                active_accounts = (await cursor.fetchone())[0]
            
            # آخرین اکانت‌ها
            async with db.execute(
                "SELECT phone, created_at FROM accounts ORDER BY created_at DESC LIMIT 5"
            ) as cursor:
                recent_accounts = await cursor.fetchall()
            
            return {
                'total_users': total_users,
                'total_accounts': total_accounts,
                'active_accounts': active_accounts,
                'recent_accounts': recent_accounts
            }
    
    async def log_action(self, action: str, user_id: Optional[int] = None, 
                        details: Optional[str] = None):
        """ثبت لاگ عملیات"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO stats (action, user_id, details) VALUES (?, ?, ?)",
                    (action, user_id, details)
                )
                await db.commit()
        except Exception as e:
            print(f"خطا در ثبت لاگ: {e}")
    
    async def add_admin(self, user_id: int) -> bool:
        """اضافه کردن ادمین"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE users SET is_admin = 1 WHERE user_id = ?",
                    (user_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در اضافه کردن ادمین: {e}")
            return False
    
    async def remove_admin(self, user_id: int) -> bool:
        """حذف ادمین"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE users SET is_admin = 0 WHERE user_id = ?",
                    (user_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در حذف ادمین: {e}")
            return False
    
    async def approve_user(self, user_id: int) -> bool:
        """تایید دسترسی کاربر"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE users SET is_approved = 1 WHERE user_id = ?",
                    (user_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در تایید کاربر: {e}")
            return False
    
    async def unapprove_user(self, user_id: int) -> bool:
        """لغو دسترسی کاربر"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE users SET is_approved = 0 WHERE user_id = ?",
                    (user_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در لغو دسترسی کاربر: {e}")
            return False
    
    async def get_pending_users(self) -> List[User]:
        """دریافت کاربران در انتظار تایید"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE is_approved = 0 AND is_admin = 0 ORDER BY created_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [User(**dict(row)) for row in rows]
    
    async def get_all_admins(self) -> List[User]:
        """دریافت لیست همه ادمین‌ها"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE is_admin = 1 ORDER BY user_id"
            ) as cursor:
                rows = await cursor.fetchall()
                return [User(**dict(row)) for row in rows]
    
    async def get_setting(self, key: str) -> Optional[str]:
        """دریافت تنظیمات"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else None
        except Exception as e:
            print(f"خطا در دریافت تنظیمات: {e}")
            return None
    
    async def set_setting(self, key: str, value: str) -> bool:
        """ذخیره تنظیمات"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO settings (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                """, (key, value))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در ذخیره تنظیمات: {e}")
            return False

    # ─── Scheduled Scenarios ────────────────────────────────────────────────

    async def add_scheduled_scenario(self, user_id: int, name: str, scenario_text: str,
                                     account_count: str, workers: int,
                                     interval_minutes: int) -> Optional[int]:
        """افزودن سناریو زمانبندی شده جدید"""
        try:
            from datetime import datetime, timedelta
            next_run = (datetime.now() + timedelta(minutes=interval_minutes)).strftime('%Y-%m-%d %H:%M:%S')
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    INSERT INTO scheduled_scenarios
                    (user_id, name, scenario_text, account_count, workers, interval_minutes, is_active, next_run_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """, (user_id, name, scenario_text, account_count, workers, interval_minutes, next_run))
                await db.commit()
                return cursor.lastrowid
        except Exception as e:
            print(f"خطا در افزودن سناریو زمانبندی: {e}")
            return None

    async def get_scheduled_scenario(self, schedule_id: int) -> Optional[Dict[str, Any]]:
        """دریافت سناریو زمانبندی شده با ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_scenarios WHERE id = ?", (schedule_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_user_scheduled_scenarios(self, user_id: int) -> List[Dict[str, Any]]:
        """دریافت لیست سناریوهای زمانبندی شده کاربر"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_scenarios WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_active_scheduled_scenarios(self) -> List[Dict[str, Any]]:
        """دریافت همه سناریوهای فعال (برای بازیابی بعد از restart)"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scheduled_scenarios WHERE is_active = 1"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def update_scheduled_last_run(self, schedule_id: int, interval_minutes: int) -> bool:
        """بروزرسانی زمان آخرین اجرا و زمان اجرای بعدی"""
        try:
            from datetime import datetime, timedelta
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            next_run = (datetime.now() + timedelta(minutes=interval_minutes)).strftime('%Y-%m-%d %H:%M:%S')
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    UPDATE scheduled_scenarios
                    SET last_run_at = ?, next_run_at = ?
                    WHERE id = ?
                """, (now, next_run, schedule_id))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در بروزرسانی آخرین اجرا: {e}")
            return False

    async def deactivate_scheduled_scenario(self, schedule_id: int) -> bool:
        """غیرفعال کردن سناریو زمانبندی شده"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE scheduled_scenarios SET is_active = 0 WHERE id = ?",
                    (schedule_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در غیرفعال کردن سناریو: {e}")
            return False

    async def delete_scheduled_scenario(self, schedule_id: int) -> bool:
        """حذف کامل سناریو زمانبندی شده"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "DELETE FROM scheduled_scenarios WHERE id = ?", (schedule_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در حذف سناریو: {e}")
            return False

    async def save_scenario_progress(self, user_id: int, scenario_text: str, 
                                     last_index: int, total: int) -> bool:
        """ذخیره پیشرفت سناریو"""
        try:
            import hashlib
            scenario_hash = hashlib.md5(scenario_text.encode()).hexdigest()
            
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO scenario_progress 
                    (user_id, scenario_hash, scenario_text, last_account_index, 
                     total_accounts, updated_at, status)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'paused')
                """, (user_id, scenario_hash, scenario_text, last_index, total))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در ذخیره پیشرفت سناریو: {e}")
            return False
    
    async def get_scenario_progress(self, user_id: int, scenario_text: str) -> Optional[Dict[str, Any]]:
        """دریافت پیشرفت سناریو"""
        if not scenario_text:
            return None
        try:
            import hashlib
            scenario_hash = hashlib.md5(scenario_text.encode()).hexdigest()
            
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM scenario_progress 
                    WHERE user_id = ? AND scenario_hash = ?
                """, (user_id, scenario_hash)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return dict(row)
                    return None
        except Exception as e:
            print(f"خطا در دریافت پیشرفت سناریو: {e}")
            return None
    
    async def delete_scenario_progress(self, user_id: int, scenario_text: str) -> bool:
        """حذف پیشرفت سناریو"""
        if not scenario_text:
            return False
        try:
            import hashlib
            scenario_hash = hashlib.md5(scenario_text.encode()).hexdigest()
            
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    DELETE FROM scenario_progress 
                    WHERE user_id = ? AND scenario_hash = ?
                """, (user_id, scenario_hash))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در حذف پیشرفت سناریو: {e}")
            return False
    
    async def get_user_scenario_progresses(self, user_id: int) -> List[Dict[str, Any]]:
        """دریافت همه پیشرفت‌های سناریوی یک کاربر"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM scenario_progress 
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                """, (user_id,)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            print(f"خطا در دریافت پیشرفت‌های سناریو: {e}")
            return []
    
    async def add_bot_note(self, user_id: int, bot_username: str, note_text: str, 
                          scenario_text: Optional[str] = None) -> bool:
        """افزودن یادداشت برای ربات"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO bot_notes (user_id, bot_username, note_text, scenario_text)
                    VALUES (?, ?, ?, ?)
                """, (user_id, bot_username, note_text, scenario_text))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در افزودن یادداشت: {e}")
            return False
    
    async def get_user_notes(self, user_id: int) -> List[Dict[str, Any]]:
        """دریافت همه یادداشت‌های یک کاربر"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM bot_notes 
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                """, (user_id,)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            print(f"خطا در دریافت یادداشت‌ها: {e}")
            return []
    
    async def get_bot_notes(self, user_id: int, bot_username: str) -> List[Dict[str, Any]]:
        """دریافت یادداشت‌های یک ربات خاص"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM bot_notes 
                    WHERE user_id = ? AND bot_username = ?
                    ORDER BY created_at DESC
                """, (user_id, bot_username)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            print(f"خطا در دریافت یادداشت‌های ربات: {e}")
            return []
    
    async def delete_note(self, note_id: int, user_id: int) -> bool:
        """حذف یادداشت"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    DELETE FROM bot_notes 
                    WHERE id = ? AND user_id = ?
                """, (note_id, user_id))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در حذف یادداشت: {e}")
            return False
    
    async def update_note(self, note_id: int, user_id: int, note_text: str) -> bool:
        """ویرایش یادداشت"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    UPDATE bot_notes 
                    SET note_text = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ?
                """, (note_text, note_id, user_id))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در ویرایش یادداشت: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # متدهای تاریخچه اجرای سناریو روی رباتها
    # ─────────────────────────────────────────────────────────────

    async def add_bot_history(self, user_id: int, bot_username: str,
                               accounts_total: int, accounts_success: int,
                               accounts_failed: int,
                               scenario_text: Optional[str] = None) -> bool:
        """ثبت یک اجرا در تاریخچه"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO bot_history
                    (user_id, bot_username, accounts_total, accounts_success,
                     accounts_failed, scenario_text)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (user_id, bot_username, accounts_total,
                      accounts_success, accounts_failed, scenario_text))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در ثبت تاریخچه: {e}")
            return False

    async def get_bot_history(self, user_id: int,
                               limit: int = 200) -> List[Dict[str, Any]]:
        """دریافت تاریخچه اجراها برای یک کاربر"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM bot_history
                    WHERE user_id = ?
                    ORDER BY executed_at DESC
                    LIMIT ?
                """, (user_id, limit)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            print(f"خطا در دریافت تاریخچه: {e}")
            return []

    async def get_bot_history_by_username(self, user_id: int,
                                           bot_username: str) -> List[Dict[str, Any]]:
        """دریافت تاریخچه اجراها برای یک ربات خاص"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT * FROM bot_history
                    WHERE user_id = ? AND bot_username = ?
                    ORDER BY executed_at DESC
                """, (user_id, bot_username)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            print(f"خطا در دریافت تاریخچه ربات: {e}")
            return []

    # ─────────────────────────────────────────────────────────────
    # متدهای مدیریت پروفایل‌های لیچ شده
    # ─────────────────────────────────────────────────────────────

    async def save_leeched_profile(
        self,
        owner_user_id: int,
        telegram_user_id: int,
        access_hash: int,
        first_name: Optional[str],
        last_name: Optional[str],
        username: Optional[str],
        bio: Optional[str],
        photos: List[bytes],          # لیست bytes عکس‌ها (حداکثر 5 تا)
        source_group: Optional[str] = None,
    ) -> Optional[int]:
        """
        ذخیره یک پروفایل لیچ شده به همراه عکس‌هایش در دیتابیس.

        Args:
            owner_user_id: آیدی ادمین/سازنده‌ای که لیچ کرده
            telegram_user_id: آیدی عددی کاربر تلگرام
            access_hash: access_hash کاربر
            first_name: نام
            last_name: نام خانوادگی
            username: یوزرنیم (بدون @)
            bio: بیو
            photos: لیست داده‌های باینری عکس‌ها (max 5)
            source_group: لینک/نام گروه منبع

        Returns:
            id پروفایل ذخیره شده یا None در صورت خطا
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    INSERT INTO leeched_profiles
                    (owner_user_id, source_group, telegram_user_id, access_hash,
                     first_name, last_name, username, bio)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (owner_user_id, source_group, telegram_user_id, access_hash,
                      first_name, last_name, username, bio))
                profile_id = cursor.lastrowid

                # ذخیره عکس‌ها (حداکثر 5 تا)
                for idx, photo_bytes in enumerate(photos[:5]):
                    await db.execute("""
                        INSERT INTO profile_photos (profile_id, photo_data, photo_index)
                        VALUES (?, ?, ?)
                    """, (profile_id, photo_bytes, idx))

                await db.commit()
                return profile_id
        except Exception as e:
            print(f"خطا در ذخیره پروفایل: {e}")
            return None

    async def get_unused_profiles(
        self,
        owner_user_id: int,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        دریافت پروفایل‌های استفاده نشده متعلق به یک ادمین/سازنده.

        Returns:
            لیست دیکشنری پروفایل‌ها (بدون داده عکس)
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT
                        p.id, p.owner_user_id, p.source_group,
                        p.telegram_user_id, p.access_hash,
                        p.first_name, p.last_name, p.username, p.bio,
                        p.is_used, p.created_at,
                        COUNT(ph.id) AS photo_count
                    FROM leeched_profiles p
                    LEFT JOIN profile_photos ph ON ph.profile_id = p.id
                    WHERE p.owner_user_id = ? AND p.is_used = 0
                    GROUP BY p.id
                    ORDER BY p.created_at DESC
                    LIMIT ?
                """, (owner_user_id, limit)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            print(f"خطا در دریافت پروفایل‌ها: {e}")
            return []

    async def get_profile_photos(self, profile_id: int) -> List[bytes]:
        """
        دریافت داده باینری عکس‌های یک پروفایل (مرتب شده بر اساس photo_index).
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT photo_data FROM profile_photos
                    WHERE profile_id = ?
                    ORDER BY photo_index ASC
                """, (profile_id,)) as cursor:
                    rows = await cursor.fetchall()
                    return [row[0] for row in rows]
        except Exception as e:
            print(f"خطا در دریافت عکس‌های پروفایل: {e}")
            return []

    async def mark_profile_used(self, profile_id: int, owner_user_id: int) -> bool:
        """
        علامت‌گذاری پروفایل به عنوان استفاده شده.
        owner_user_id برای جلوگیری از دسترسی متقاطع بین ادمین‌ها چک می‌شود.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    UPDATE leeched_profiles
                    SET is_used = 1
                    WHERE id = ? AND owner_user_id = ?
                """, (profile_id, owner_user_id))
                await db.commit()
                return True
        except Exception as e:
            print(f"خطا در علامت‌گذاری پروفایل: {e}")
            return False

    async def get_profiles_stats(self, owner_user_id: int) -> Dict[str, int]:
        """آمار پروفایل‌های یک ادمین/سازنده."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN is_used = 0 THEN 1 ELSE 0 END) AS unused,
                        SUM(CASE WHEN is_used = 1 THEN 1 ELSE 0 END) AS used
                    FROM leeched_profiles
                    WHERE owner_user_id = ?
                """, (owner_user_id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return {'total': row[0] or 0, 'unused': row[1] or 0, 'used': row[2] or 0}
                    return {'total': 0, 'unused': 0, 'used': 0}
        except Exception as e:
            print(f"خطا در دریافت آمار پروفایل‌ها: {e}")
            return {'total': 0, 'unused': 0, 'used': 0}

    async def delete_used_profiles(self, owner_user_id: int) -> int:
        """حذف پروفایل‌های استفاده شده. برمی‌گرداند تعداد حذف شده."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    DELETE FROM leeched_profiles
                    WHERE owner_user_id = ? AND is_used = 1
                """, (owner_user_id,))
                await db.commit()
                return cursor.rowcount
        except Exception as e:
            print(f"خطا در حذف پروفایل‌های استفاده شده: {e}")
            return 0

    async def delete_all_profiles(self, owner_user_id: int) -> int:
        """حذف همه پروفایل‌های یک ادمین/سازنده."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    DELETE FROM leeched_profiles WHERE owner_user_id = ?
                """, (owner_user_id,))
                await db.commit()
                return cursor.rowcount
        except Exception as e:
            print(f"خطا در حذف پروفایل‌ها: {e}")
            return 0

    async def profile_already_leeched(
        self, owner_user_id: int, telegram_user_id: int
    ) -> bool:
        """بررسی اینکه آیا این پروفایل قبلاً برای این ادمین لیچ شده."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT id FROM leeched_profiles
                    WHERE owner_user_id = ? AND telegram_user_id = ?
                    LIMIT 1
                """, (owner_user_id, telegram_user_id)) as cursor:
                    return await cursor.fetchone() is not None
        except Exception as e:
            print(f"خطا در بررسی تکراری بودن پروفایل: {e}")
            return False
