"""هندلر ربات تلگرام"""
import logging
import asyncio
import random
import string
from datetime import datetime
from pathlib import Path
from telethon import TelegramClient, events
from telethon.tl.custom import Button

from src.config import Config
from src.services import AccountReceiver, ChannelManager, ReferralManager, MessageSender, BotAutomation, BackupManager, ReactionManager, BlockManager, NoteManager
from src.services.pishi_service import PishiService
from src.models import AccountCredentials
from src.database import Database, User, Account
from src.utils.validators import extract_telegram_code

logger = logging.getLogger(__name__)

class BotHandler:
    """کلاس مدیریت ربات تلگرام"""
    
    def __init__(self):
        """مقداردهی اولیه"""
        self.bot = TelegramClient(
            'bot_session',
            Config.API_ID,
            Config.API_HASH
        )
        self.receiver = AccountReceiver()
        self.channel_manager = ChannelManager()
        self.referral_manager = ReferralManager()
        self.message_sender = MessageSender()
        self.bot_automation = BotAutomation()
        self.backup_manager = BackupManager()
        self.reaction_manager = ReactionManager()
        self.block_manager = BlockManager()
        self.db = Database(Config.DATABASE_PATH)
        self.note_manager = NoteManager(self.db)
        self.pishi_service = PishiService()
        
        # ذخیره وضعیت کاربران
        self.user_states = {}
        
        # ذخیره وضعیت عملیات‌های در حال اجرا (برای لغو)
        self.running_operations = {}
        
        # قفل سشن‌ها - برای جلوگیری از استفاده همزمان از یک سشن
        self.session_locks = set()  # مجموعه session_path های در حال استفاده

        # سناریوهای زمانبندی شده: schedule_id → asyncio.Task
        self.scheduled_tasks: dict = {}

        # اتوماسیون پیشی: account_id → asyncio.Task
        self.pishi_automation_tasks: dict = {}
    
    async def _handle_invalid_session(self, session_path: str, account=None):
        """
        غیرفعال کردن سشن نامعتبر در همه سرویس‌ها
        
        Args:
            session_path: مسیر فایل سشن
            account: آبجکت اکانت (اختیاری)
        """
        try:
            await self.db.invalidate_session(session_path)
            logger.warning(f"سشن نامعتبر غیرفعال و منتقل شد: {session_path}")
        except Exception as e:
            logger.error(f"خطا در غیرفعال کردن سشن: {e}")
    
    async def _check_and_invalidate(self, result: dict, session_path: str) -> bool:
        """
        بررسی نتیجه و غیرفعال کردن سشن نامعتبر اگر لازم بود
        
        Returns:
            True اگر سشن نامعتبر بود
        """
        msg = result.get('message', '')
        invalid_keywords = ['سشن نامعتبر', 'SESSION_REVOKED', 'AUTH_KEY_UNREGISTERED',
                           'USER_DEACTIVATED', 'SESSION_EXPIRED', 'unauthorized']
        
        if not result.get('success') and any(k.lower() in msg.lower() for k in invalid_keywords):
            await self._handle_invalid_session(session_path)
            return True
        return False
    
    async def _process_bulk_results_for_invalid_sessions(
        self, results: dict, accounts: list
    ) -> int:
        """
        بررسی نتایج bulk و غیرفعال کردن سشن‌های نامعتبر
        
        Args:
            results: نتایج bulk operation
            accounts: لیست اکانت‌ها
            
        Returns:
            تعداد سشن‌های نامعتبر شناسایی شده
        """
        invalid_count = 0
        invalid_keywords = [
            'سشن نامعتبر', 'SESSION_REVOKED', 'AUTH_KEY_UNREGISTERED',
            'USER_DEACTIVATED', 'SESSION_EXPIRED', 'unauthorized'
        ]
        
        for i, detail in enumerate(results.get('details', [])):
            result = detail.get('result', {})
            if not result.get('success'):
                msg = result.get('message', '')
                if any(k.lower() in msg.lower() for k in invalid_keywords):
                    # پیدا کردن session_path برای این اکانت
                    if i < len(accounts) and accounts[i].session_path:
                        await self.db.invalidate_session(accounts[i].session_path)
                        invalid_count += 1
        
        return invalid_count
    
    def _lock_sessions(self, session_paths: list) -> tuple:
        """
        قفل کردن سشن‌ها برای استفاده
        
        Args:
            session_paths: لیست مسیر سشن‌ها
            
        Returns:
            (available_sessions, locked_sessions)
        """
        available = []
        locked = []
        
        for path in session_paths:
            if path not in self.session_locks:
                available.append(path)
                self.session_locks.add(path)
            else:
                locked.append(path)
        
        return available, locked
    
    def _unlock_sessions(self, session_paths: list):
        """
        آزاد کردن سشن‌ها بعد از استفاده
        
        Args:
            session_paths: لیست مسیر سشن‌ها
        """
        for path in session_paths:
            self.session_locks.discard(path)
    
    async def _ask_account_count(self, event, user_id, total_accounts: int, next_step: str, operation_name: str):
        """
        پرسیدن تعداد اکانت از کاربر
        
        Args:
            event: رویداد تلگرام
            user_id: آیدی کاربر
            total_accounts: تعداد کل اکانت‌های فعال
            next_step: مرحله بعدی
            operation_name: نام عملیات (برای نمایش)
        """
        self.user_states[user_id]['step'] = next_step
        
        await event.respond(
            f"📊 **انتخاب تعداد اکانت**\n\n"
            f"شما {total_accounts} اکانت فعال دارید.\n\n"
            f"چند تا اکانت برای {operation_name} استفاده شود؟\n\n"
            f"💡 عدد ارسال کنید (مثلاً 5) یا:\n"
            f"• /all برای همه اکانت‌ها",
            buttons=Button.inline("❌ لغو", b"cancel")
        )
    
    async def _ask_workers_count(self, event, user_id, total_accounts: int, next_step: str):
        """
        پرسیدن تعداد worker از کاربر
        
        Args:
            event: رویداد تلگرام
            user_id: آیدی کاربر
            total_accounts: تعداد اکانت‌های انتخاب شده
            next_step: مرحله بعدی
        """
        self.user_states[user_id]['step'] = next_step
        
        # محاسبه زمان تخمینی
        avg_delay = Config.DELAY_BETWEEN_ACTIONS + (Config.DELAY_RANDOM_RANGE / 2)
        estimated_minutes_1 = int((total_accounts * avg_delay) / 60)
        estimated_minutes_3 = int((total_accounts * avg_delay) / 60 / 3)
        
        await event.respond(
            f"⚡ **سرعت اجرا**\n\n"
            f"📊 تعداد اکانت‌های انتخاب شده: {total_accounts}\n\n"
            f"چند تا اکانت همزمان اجرا شوند؟\n\n"
            f"💡 **توصیه:**\n"
            f"• `1` - یکی یکی (~{estimated_minutes_1} دقیقه) ✅\n"
            f"• `3` - 3 تا همزمان (~{estimated_minutes_3} دقیقه)\n"
            f"• `5` - 5 تا همزمان (سریع‌تر)\n"
            f"• `10` - 10 تا همزمان (خیلی سریع)\n\n"
            f"⚠️ **نکته:** هرچه عدد بیشتر، سریع‌تر ولی فشار بیشتر",
            buttons=Button.inline("❌ لغو", b"cancel")
        )
    
    async def _ask_time_limit(self, event, user_id, total_accounts: int, workers: int, next_step: str):
        """
        پرسیدن بازه زمانی از کاربر
        
        Args:
            event: رویداد تلگرام
            user_id: آیدی کاربر
            total_accounts: تعداد اکانت‌های انتخاب شده
            workers: تعداد worker
            next_step: مرحله بعدی
        """
        self.user_states[user_id]['step'] = next_step
        
        # محاسبه زمان تخمینی با تاخیر فعلی
        avg_delay = Config.DELAY_BETWEEN_ACTIONS + (Config.DELAY_RANDOM_RANGE / 2)
        estimated_minutes = int((total_accounts * avg_delay) / 60 / workers)
        
        await event.respond(
            f"⏰ **بازه زمانی اجرا**\n\n"
            f"📊 تعداد اکانت‌ها: {total_accounts}\n"
            f"⚡ همزمان: {workers} اکانت\n"
            f"⏱ زمان تخمینی با تاخیر فعلی: ~{estimated_minutes} دقیقه\n\n"
            f"💡 **می‌خواهید عملیات در چه بازه زمانی تموم شود؟**\n\n"
            f"**گزینه‌ها:**\n"
            f"• `/skip` - بدون محدودیت زمانی (با تاخیر فعلی)\n"
            f"• عدد دقیقه ارسال کنید (مثلاً `30` برای 30 دقیقه)\n"
            f"• یا فرمت ساعت:دقیقه (مثلاً `1:30` برای 1 ساعت و 30 دقیقه)\n\n"
            f"⚠️ **نکته:** سیستم خودکار تاخیر بین اکانت‌ها را محاسبه می‌کند",
            buttons=[[
                Button.inline("⏩ بدون محدودیت", b"skip_time_limit"),
                Button.inline("❌ لغو", b"cancel")
            ]]
        )
    
    def _parse_time_limit(self, text: str, total_accounts: int, workers: int) -> tuple:
        """
        پارس کردن بازه زمانی و محاسبه تاخیر
        
        Args:
            text: ورودی کاربر
            total_accounts: تعداد اکانت‌ها
            workers: تعداد worker
            
        Returns:
            (custom_delay, time_limit_text) یا (None, "") اگر skip باشه
            
        Raises:
            ValueError: اگر ورودی نامعتبر باشد
        """
        if text.lower() == '/skip':
            return None, ""
        
        # پارس کردن ورودی
        total_minutes = 0
        
        if ':' in text:
            # فرمت ساعت:دقیقه
            parts = text.split(':')
            hours = int(parts[0])
            minutes = int(parts[1]) if len(parts) > 1 else 0
            total_minutes = (hours * 60) + minutes
        else:
            # فقط دقیقه
            total_minutes = int(text)
        
        if total_minutes < 1:
            raise ValueError("بازه زمانی باید حداقل 1 دقیقه باشد")
        
        # محاسبه تاخیر مورد نیاز
        total_seconds = total_minutes * 60
        calculated_delay = (total_seconds / total_accounts) * workers
        
        # حداقل 1 ثانیه
        if calculated_delay < 1:
            min_minutes = int((total_accounts / workers) / 60) + 1
            raise ValueError(f"بازه زمانی خیلی کم است! حداقل {min_minutes} دقیقه نیاز است")
        
        custom_delay = int(calculated_delay)
        
        # نمایش زمان به فرمت خوانا
        if total_minutes >= 60:
            hours = total_minutes // 60
            mins = total_minutes % 60
            time_limit_text = f"⏰ بازه زمانی: {hours} ساعت و {mins} دقیقه\n"
        else:
            time_limit_text = f"⏰ بازه زمانی: {total_minutes} دقیقه\n"
        
        time_limit_text += f"⏱ تاخیر محاسبه شده: ~{custom_delay} ثانیه بین هر اکانت\n"
        
        return custom_delay, time_limit_text
    
    def _select_accounts(self, count_input: str, all_accounts: list) -> list:
        """
        انتخاب تعداد مشخصی از اکانت‌ها
        
        Args:
            count_input: ورودی کاربر (عدد یا /all)
            all_accounts: لیست همه اکانت‌ها
            
        Returns:
            لیست اکانت‌های انتخاب شده
            
        Raises:
            ValueError: اگر ورودی نامعتبر باشد
        """
        if count_input.lower() == '/all':
            return all_accounts
        
        count = int(count_input)
        if count <= 0:
            raise ValueError("تعداد باید بیشتر از صفر باشد")
        
        return all_accounts[:min(count, len(all_accounts))]
    
    def _create_country_buttons(self, countries: list) -> tuple:
        """
        ساخت دکمه‌های شیشه‌ای برای انتخاب کشور
        
        Args:
            countries: لیست کشورها با تعداد اکانت‌ها
            
        Returns:
            (متن پیام, دکمه‌ها)
        """
        from src.utils.countries import get_country_flag
        from telethon import Button
        
        country_text = "🌍 **انتخاب کشور برای اجرای سناریو**\n\n"
        country_text += f"📊 شما اکانت از **{len(countries)} کشور** مختلف دارید\n\n"
        country_text += f"💡 روی کشور مورد نظر کلیک کنید:\n"
        
        # ساخت دکمه‌ها
        buttons = []
        
        # دکمه‌های کشورها (2 تا در هر ردیف)
        for i in range(0, len(countries), 2):
            row = []
            for j in range(2):
                if i + j < len(countries):
                    country = countries[i + j]
                    flag = get_country_flag(country['country_code'])
                    label = f"{flag} {country['country_code']} ({country['count']})"
                    callback_data = f"country_{country['country_code']}"
                    row.append(Button.inline(label, callback_data.encode()))
            buttons.append(row)
        
        # دکمه همه کشورها
        total_accounts = sum(c['count'] for c in countries)
        buttons.append([Button.inline(f"🌍 همه کشورها ({total_accounts} اکانت)", b"all_countries")])
        
        # دکمه لغو
        buttons.append([Button.inline("❌ لغو", b"cancel")])
        
        return country_text, buttons
    
    async def _check_admin_access(self, event) -> bool:
        """
        بررسی دسترسی ادمین
        
        Args:
            event: رویداد تلگرام
            
        Returns:
            True اگر کاربر ادمین یا سازنده باشد
        """
        user_id = event.sender_id
        is_creator = user_id in Config.ADMIN_IDS
        is_admin = await self.db.is_admin(user_id)
        
        if not is_creator and not is_admin:
            await event.answer("⛔️ این قابلیت فقط برای ادمین‌ها در دسترس است!", alert=True)
            return False
        
        return True
    
    async def _check_creator_access(self, event) -> bool:
        """
        بررسی دسترسی سازنده
        
        Args:
            event: رویداد تلگرام
            
        Returns:
            True اگر کاربر سازنده باشد
        """
        user_id = event.sender_id
        is_creator = user_id in Config.ADMIN_IDS
        
        if not is_creator:
            await event.answer("⛔️ این قابلیت فقط برای سازنده در دسترس است!", alert=True)
            return False
        
        return True
    
    async def _apply_profiles_handler(self, event, apply_photo: bool, apply_bio: bool, apply_username: bool):
        """
        هندلر اعمال پروفایل‌ها
        
        Args:
            event: رویداد callback
            apply_photo: اعمال عکس پروفایل
            apply_bio: اعمال بیو
            apply_username: اعمال یوزرنیم
        """
        user_id = event.sender_id
        
        if user_id not in self.user_states:
            await event.answer("❌ خطا! لطفاً دوباره تلاش کنید.", alert=True)
            return
        
        await event.answer()
        
        state = self.user_states[user_id]
        members = state.get('leech_members', [])
        accounts = state.get('leech_accounts', [])
        
        if not members or not accounts:
            await event.edit(
                "❌ اطلاعات لیچ پیدا نشد.",
                buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
            )
            return
        
        # نمایش پیام شروع
        progress_msg = await event.edit(
            f"🔄 **در حال اعمال پروفایل‌ها...**\n\n"
            f"👥 تعداد اکانت: {len(accounts)}\n"
            f"📊 تعداد پروفایل: {len(members)}\n\n"
            f"⏳ لطفاً صبر کنید...",
            buttons=Button.inline("❌ لغو", b"cancel_scenario")
        )
        
        # ذخیره flag برای لغو
        cancel_flag = {'cancelled': False}
        self.running_operations[user_id] = cancel_flag
        
        try:
            # تابع callback برای نمایش پیشرفت
            async def progress_callback(current, total, message):
                try:
                    await progress_msg.edit(
                        f"🔄 **در حال اعمال پروفایل‌ها...**\n\n"
                        f"👥 اکانت: {current}/{total}\n"
                        f"📊 {message}",
                        buttons=Button.inline("❌ لغو", b"cancel_scenario")
                    )
                except Exception as e:
                    logger.error(f"خطا در بروزرسانی پیشرفت: {e}")
            
            # اعمال پروفایل‌ها
            from src.services.profile_applier import ProfileApplier
            applier = ProfileApplier()
            
            session_paths = [acc.session_path for acc in accounts]
            
            # شافل کردن اعضا برای تصادفی بودن
            import random
            shuffled_members = members.copy()
            random.shuffle(shuffled_members)
            
            results = await applier.bulk_apply(
                session_paths,
                shuffled_members,
                apply_photo=apply_photo,
                apply_bio=apply_bio,
                apply_username=apply_username,
                progress_callback=progress_callback,
                cancel_flag=cancel_flag
            )
            
            # حذف flag
            if user_id in self.running_operations:
                del self.running_operations[user_id]
            
            # بررسی لغو
            if cancel_flag.get('cancelled'):
                await progress_msg.edit(
                    "❌ **اعمال پروفایل لغو شد!**",
                    buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                )
                del self.user_states[user_id]
                return
            
            # نمایش نتیجه
            await progress_msg.edit(
                f"✅ **اعمال پروفایل تکمیل شد!**\n\n"
                f"📊 **آمار:**\n"
                f"• موفق: {results['success_count']}\n"
                f"• ناموفق: {results['failed_count']}\n"
                f"• کل: {results['total_accounts']}\n\n"
                f"💡 پروفایل‌های جدید روی اکانت‌ها اعمال شد.",
                buttons=[
                    [Button.inline("👥 لیچ جدید", b"leecher")],
                    [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                ]
            )
            
            # پاک کردن state
            del self.user_states[user_id]
        
        except Exception as e:
            logger.exception(f"خطا در اعمال پروفایل: {e}")
            
            await progress_msg.edit(
                f"❌ **خطا در اعمال پروفایل!**\n\n"
                f"خطا: {str(e)[:100]}",
                buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
            )
            
            # پاک کردن state و flag
            if user_id in self.user_states:
                del self.user_states[user_id]
            if user_id in self.running_operations:
                del self.running_operations[user_id]
    
    async def _apply_from_excel_handler(self, event, apply_photo: bool, apply_bio: bool, apply_username: bool):
        """هندلر اعمال پروفایل از دیتابیس"""
        user_id = event.sender_id

        if user_id not in self.user_states:
            await event.answer("❌ خطا! لطفاً دوباره تلاش کنید.", alert=True)
            return

        await event.answer()

        state = self.user_states[user_id]
        selected_accounts = state.get('selected_accounts', [])

        if not selected_accounts:
            await event.edit(
                "❌ اطلاعات اکانت‌ها پیدا نشد.",
                buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
            )
            return

        progress_msg = await event.edit(
            f"🔄 **در حال اعمال پروفایل‌ها...**\n\n"
            f"👥 تعداد اکانت: {len(selected_accounts)}\n"
            f"🗄 منبع: دیتابیس پروفایل‌های شما\n\n"
            f"⏳ لطفاً صبر کنید...",
            buttons=Button.inline("❌ لغو", b"cancel_scenario")
        )

        cancel_flag = {'cancelled': False}
        self.running_operations[user_id] = cancel_flag

        try:
            async def progress_callback(current, total, message):
                try:
                    await progress_msg.edit(
                        f"🔄 **در حال اعمال پروفایل‌ها...**\n\n"
                        f"👥 اکانت: {current}/{total}\n"
                        f"📊 {message}",
                        buttons=Button.inline("❌ لغو", b"cancel_scenario")
                    )
                except Exception as e:
                    logger.error(f"خطا در بروزرسانی پیشرفت: {e}")

            from src.services.profile_applier import ProfileApplier
            applier = ProfileApplier()

            session_paths = [acc.session_path for acc in selected_accounts]

            results = await applier.bulk_apply_from_db(
                session_paths=session_paths,
                owner_user_id=user_id,
                db=self.db,
                apply_photo=apply_photo,
                apply_bio=apply_bio,
                apply_username=apply_username,
                progress_callback=progress_callback,
                cancel_flag=cancel_flag,
            )

            if user_id in self.running_operations:
                del self.running_operations[user_id]

            if cancel_flag.get('cancelled'):
                await progress_msg.edit(
                    "❌ **اعمال پروفایل لغو شد!**",
                    buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                )
                del self.user_states[user_id]
                return

            stats = await self.db.get_profiles_stats(user_id)

            # ── خلاصه نتیجه (۵ اکانت اول) ──────────────────────
            details = results.get('details', [])
            preview_lines = []
            for d in details[:5]:
                icon = "✅" if d['status'] == 'ok' else "❌"
                acc_user_str = f"@{d['acc_user']}" if d['acc_user'] != '—' else 'بدون یوزرنیم'
                line = f"{icon} `{d['acc_phone']}` | ID: `{d['acc_id']}` | {acc_user_str}"
                line += f"\n     🎭 منبع ID: `{d['src_id']}` ← **{d['new_name']}**"
                if d['new_user'] != '—':
                    line += f" | @{d['new_user']}"
                line += f"\n     📦 {d['applied']}"
                preview_lines.append(line)
            preview_text = "\n".join(preview_lines)
            if len(details) > 5:
                preview_text += f"\n_... و {len(details) - 5} اکانت دیگر_"

            result_msg = (
                f"✅ **اعمال پروفایل تکمیل شد!**\n\n"
                f"📊 **نتیجه:**\n"
                f"• موفق: {results['success_count']}\n"
                f"• ناموفق: {results['failed_count']}\n"
                f"• بدون پروفایل: {results['no_profile_count']}\n\n"
                f"🗄 **پروفایل‌های باقی‌مانده:**\n"
                f"• استفاده نشده: {stats['unused']}\n"
                f"• استفاده شده: {stats['used']}\n\n"
                f"📋 **نمونه تغییرات:**\n{preview_text}"
            )

            await progress_msg.edit(
                result_msg,
                buttons=[
                    [Button.inline("📄 گزارش کامل TXT", b"apply_report_txt")],
                    [Button.inline("🎨 اعمال مجدد", b"apply_profiles")],
                    [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                ]
            )

            # ذخیره details در state برای دکمه گزارش
            self.user_states[user_id] = {
                'step': 'apply_done',
                'apply_details': details,
            }

        except Exception as e:
            logger.exception(f"خطا در اعمال پروفایل: {e}")
            await progress_msg.edit(
                f"❌ **خطا در اعمال پروفایل!**\n\nخطا: {str(e)[:100]}",
                buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
            )
            if user_id in self.user_states:
                del self.user_states[user_id]
            if user_id in self.running_operations:
                del self.running_operations[user_id]
    
    async def _run_pishi_panel(self, user_id: int):
        """
        Loop اتوماسیون پیشی per-user.
        برای هر تسک (میو/ماهی/انتقال):
        - همه N اکانت انتخاب شده را در بازه زمانی پخش می‌کنه
        - تاخیر بین هر اکانت = interval / N (حداقل ۵ ثانیه)
        - بعد از اتمام دور، صبر تا بازه بعدی
        """
        import time
        from datetime import datetime

        logger.info(f"[pishi_panel] شروع اتوماسیون user={user_id}")

        # زمان آخرین دور کامل برای هر تسک
        last_mio_round:  float = 0.0
        last_fish_round: float = 0.0
        last_transfer_date = None

        while True:
            try:
                cfg = await self.db.get_pishi_panel(user_id)
                if not cfg or not cfg['is_running']:
                    logger.info(f"[pishi_panel] user={user_id} متوقف شد")
                    break

                accounts_all = await self.db.get_accounts(user_id)
                active = [a for a in accounts_all if a.status == 'active' and a.session_path]
                count = cfg.get('account_count') or len(active)
                try:
                    count = int(count)
                except (TypeError, ValueError):
                    count = len(active)
                selected = active[:min(count, len(active))]

                if not selected:
                    await asyncio.sleep(60)
                    continue

                group        = cfg['group_username']
                now          = time.time()
                today        = datetime.now().date()
                current_hour = datetime.now().hour
                n            = len(selected)

                mio_interval_sec  = cfg['mio_interval_minutes'] * 60
                fish_interval_sec = cfg['fish_interval_minutes'] * 60

                # تاخیر بین اکانت‌ها = interval ÷ N (حداقل ۵ ثانیه)
                mio_delay  = max(5, mio_interval_sec  // n)
                fish_delay = max(5, fish_interval_sec // n)

                # ─── میو ──────────────────────────────────────────────
                if cfg['mio_enabled'] and now - last_mio_round >= mio_interval_sec:
                    logger.info(f"[pishi/mio] شروع دور جدید — {n} اکانت، تاخیر {mio_delay}s")
                    for acc in selected:
                        try:
                            r = await self.pishi_service.send_mio(acc.session_path, group)
                            s = '✅' if r['success'] else f"❌ {r['message'][:25]}"
                            logger.info(f"[pishi/mio] {acc.phone} {s}")
                        except Exception as e:
                            logger.error(f"[pishi/mio] {acc.phone}: {e}")
                        await asyncio.sleep(mio_delay)
                    last_mio_round = time.time()

                # ─── ماهی ─────────────────────────────────────────────
                if cfg['fish_enabled'] and now - last_fish_round >= fish_interval_sec:
                    logger.info(f"[pishi/fish] شروع دور جدید — {n} اکانت، تاخیر {fish_delay}s")
                    for acc in selected:
                        try:
                            r = await self.pishi_service.send_fish_and_click(
                                acc.session_path, group, button_index=1)
                            s = '✅' if r['success'] else f"❌ {r['message'][:25]}"
                            logger.info(f"[pishi/fish] {acc.phone} {s}")
                        except Exception as e:
                            logger.error(f"[pishi/fish] {acc.phone}: {e}")
                        await asyncio.sleep(fish_delay)
                    last_fish_round = time.time()

                # ─── انتقال موجودی ────────────────────────────────────
                if cfg['transfer_enabled'] and cfg.get('transfer_target_msg_id'):
                    transfer_hour = cfg.get('transfer_hour', 0)
                    if last_transfer_date != today and current_hour == transfer_hour:
                        logger.info(f"[pishi/transfer] شروع دور انتقال — {n} اکانت")
                        transfer_delay = max(10, 3600 // n)
                        for acc in selected:
                            try:
                                r = await self.pishi_service.transfer_balance(
                                    acc.session_path, group,
                                    cfg['transfer_target_msg_id'])
                                s = '✅' if r.get('final_success') else f"⚠️ {r['message'][:25]}"
                                logger.info(f"[pishi/transfer] {acc.phone} {s}")
                            except Exception as e:
                                logger.error(f"[pishi/transfer] {acc.phone}: {e}")
                            await asyncio.sleep(transfer_delay)
                        last_transfer_date = today

                # هر ۳۰ ثانیه تایمر رو چک کن
                await asyncio.sleep(30)

            except asyncio.CancelledError:
                logger.info(f"[pishi_panel] user={user_id} لغو شد")
                break
            except Exception as e:
                logger.error(f"[pishi_panel] user={user_id} خطا: {e}")
                await asyncio.sleep(60)

    async def _run_pishi_automation(self, account_id: int, user_id: int):
        """
        Loop اتوماسیون پیشی برای یک اکانت:
        - هر N دقیقه: میو
        - هر M دقیقه: ماهی
        - هر روز ساعت H: انتقال موجودی
        """
        import time
        from datetime import datetime

        logger.info(f"[pishi_auto] شروع اتوماسیون account_id={account_id}")

        last_mio: float = 0.0
        last_fish: float = 0.0
        last_transfer_date = None

        while True:
            try:
                cfg = await self.db.get_pishi_automation(user_id, account_id)
                if not cfg or not cfg['is_running']:
                    logger.info(f"[pishi_auto] account_id={account_id} متوقف شد")
                    break

                # دریافت اطلاعات اکانت
                accounts = await self.db.get_accounts(user_id)
                account = next((a for a in accounts if a.id == account_id), None)
                if not account or account.status != 'active' or not account.session_path:
                    logger.warning(f"[pishi_auto] account_id={account_id} غیرفعال یا session ندارد")
                    await asyncio.sleep(120)
                    continue

                now = time.time()
                today = datetime.now().date()
                current_hour = datetime.now().hour
                group = cfg['group_username']

                # ─── میو ───────────────────────────────────────────────
                if cfg['mio_enabled']:
                    if now - last_mio >= cfg['mio_interval_minutes'] * 60:
                        try:
                            result = await self.pishi_service.send_mio(
                                account.session_path, group
                            )
                            status = '✅' if result['success'] else '❌'
                            logger.info(f"[pishi_auto/mio] acc={account.phone} {status} {result['message'][:40]}")
                            last_mio = time.time()
                        except Exception as e:
                            logger.error(f"[pishi_auto/mio] خطا: {e}")
                            last_mio = time.time()  # از loop بی‌نهایت جلوگیری کن

                # ─── ماهی ──────────────────────────────────────────────
                if cfg['fish_enabled']:
                    if now - last_fish >= cfg['fish_interval_minutes'] * 60:
                        try:
                            result = await self.pishi_service.send_fish_and_click(
                                account.session_path, group, button_index=1
                            )
                            status = '✅' if result['success'] else '❌'
                            logger.info(f"[pishi_auto/fish] acc={account.phone} {status} {result['message'][:40]}")
                            last_fish = time.time()
                        except Exception as e:
                            logger.error(f"[pishi_auto/fish] خطا: {e}")
                            last_fish = time.time()

                # ─── انتقال موجودی ─────────────────────────────────────
                if cfg['transfer_enabled'] and cfg.get('transfer_target_msg_id'):
                    transfer_hour = cfg.get('transfer_hour', 0)
                    if last_transfer_date != today and current_hour == transfer_hour:
                        try:
                            result = await self.pishi_service.transfer_balance(
                                account.session_path, group, cfg['transfer_target_msg_id']
                            )
                            status = '✅' if result.get('final_success') else '⚠️'
                            logger.info(f"[pishi_auto/transfer] acc={account.phone} {status} {result['message'][:40]}")
                            last_transfer_date = today
                        except Exception as e:
                            logger.error(f"[pishi_auto/transfer] خطا: {e}")
                            last_transfer_date = today  # فقط یه بار تلاش کن

                # هر ۳۰ ثانیه وضعیت رو چک کن
                await asyncio.sleep(30)

            except asyncio.CancelledError:
                logger.info(f"[pishi_auto] account_id={account_id} لغو شد")
                break
            except Exception as e:
                logger.error(f"[pishi_auto] خطای غیرمنتظره account_id={account_id}: {e}")
                await asyncio.sleep(60)

    async def _run_scheduled_scenario(self, schedule_id: int):
        """
        حلقه اجرای سناریو زمانبندی شده — تا زمانی که is_active باشه تکرار میشه
        """
        logger.info(f"شروع سناریو زمانبندی شده #{schedule_id}")
        
        while True:
            try:
                row = await self.db.get_scheduled_scenario(schedule_id)
                if not row or not row['is_active']:
                    logger.info(f"سناریو زمانبندی #{schedule_id} غیرفعال شد، متوقف می‌شود")
                    break
                
                user_id = row['user_id']
                scenario_text = row['scenario_text']
                account_count_str = row['account_count']
                workers = row['workers']
                interval_minutes = row['interval_minutes']
                name = row['name']
                
                logger.info(f"اجرای سناریو زمانبندی #{schedule_id} '{name}' برای user {user_id}")
                
                # دریافت اکانت‌های فعال
                accounts = await self.db.get_accounts(user_id)
                active_accounts = [a for a in accounts if a.status == 'active' and a.session_path]
                
                if not active_accounts:
                    logger.warning(f"سناریو زمانبندی #{schedule_id}: هیچ اکانت فعالی نیست")
                else:
                    # انتخاب اکانت‌ها بر اساس account_count
                    try:
                        selected = self._select_accounts(account_count_str, active_accounts)
                    except Exception:
                        selected = active_accounts
                    
                    # اجرای سناریو
                    try:
                        lines = scenario_text.split('\n')
                        bot_count = sum(1 for line in lines if line.strip().startswith('@'))
                        
                        if bot_count > 1:
                            bots_scenarios = self.bot_automation.parse_multi_bot_scenario(scenario_text)
                        else:
                            first_line = next((l.strip() for l in lines if l.strip().startswith('@')), '')
                            bot_username = first_line.lstrip('@')
                            scenario_lines = [l for l in lines if not l.strip().startswith('@')]
                            parsed_scenario = self.bot_automation.parse_scenario('\n'.join(scenario_lines))
                            bots_scenarios = [{'bot_username': bot_username, 'scenario': parsed_scenario}]
                        
                        success_count = 0
                        fail_count = 0
                        
                        for account in selected:
                            for bot_data in bots_scenarios:
                                try:
                                    result = await self.bot_automation.execute_scenario(
                                        account.session_path,
                                        bot_data['bot_username'],
                                        bot_data['scenario'],
                                        db=self.db
                                    )
                                    if result.get('success'):
                                        success_count += 1
                                    else:
                                        fail_count += 1
                                except Exception as e:
                                    fail_count += 1
                                    logger.error(f"خطا در اجرای scheduled scenario #{schedule_id}: {e}")
                            
                            await asyncio.sleep(2)  # تاخیر کوچک بین اکانت‌ها
                        
                        # اطلاع‌رسانی به کاربر
                        try:
                            from datetime import datetime
                            now_str = datetime.now().strftime('%H:%M:%S')
                            await self.bot.send_message(
                                user_id,
                                f"⏰ **سناریو زمانبندی اجرا شد**\n\n"
                                f"📌 نام: {name}\n"
                                f"🕐 زمان: {now_str}\n"
                                f"✅ موفق: {success_count}\n"
                                f"❌ ناموفق: {fail_count}\n"
                                f"⏳ اجرای بعدی: {interval_minutes} دقیقه دیگر"
                            )
                        except Exception:
                            pass
                        
                        await self.db.update_scheduled_last_run(schedule_id, interval_minutes)
                        logger.info(f"سناریو زمانبندی #{schedule_id} اجرا شد: {success_count} موفق، {fail_count} ناموفق")
                        
                    except Exception as e:
                        logger.error(f"خطا در اجرای سناریو زمانبندی #{schedule_id}: {e}")
                
                # صبر تا اجرای بعدی
                await asyncio.sleep(interval_minutes * 60)
                
            except asyncio.CancelledError:
                logger.info(f"سناریو زمانبندی #{schedule_id} لغو شد")
                break
            except Exception as e:
                logger.error(f"خطا در حلقه scheduled #{schedule_id}: {e}")
                await asyncio.sleep(60)  # یه دقیقه صبر و دوباره تلاش

    async def start(self):
        """راه‌اندازی ربات"""
        # راه‌اندازی دیتابیس
        await self.db.init_db()
        
        # افزودن ادمین‌ها
        for admin_id in Config.ADMIN_IDS:
            await self.db.add_user(User(
                user_id=admin_id,
                is_admin=True
            ))
        
        # بارگذاری کانال بکاپ از دیتابیس
        backup_channel = await self.db.get_setting('backup_channel_id')
        if backup_channel:
            self.backup_manager.set_backup_channel(int(backup_channel))
            logger.info(f"کانال بکاپ از دیتابیس بارگذاری شد: {backup_channel}")
        
        await self.bot.start(bot_token=Config.BOT_TOKEN)
        
        logger.info("ربات راه‌اندازی شد")
        
        # بازیابی سناریوهای زمانبندی شده فعال بعد از restart
        try:
            active_schedules = await self.db.get_all_active_scheduled_scenarios()
            for sched in active_schedules:
                sid = sched['id']
                task = asyncio.create_task(self._run_scheduled_scenario(sid))
                self.scheduled_tasks[sid] = task
                logger.info(f"سناریو زمانبندی #{sid} '{sched['name']}' بازیابی شد")
        except Exception as e:
            logger.error(f"خطا در بازیابی سناریوهای زمانبندی: {e}")

        # بازیابی اتوماسیون‌های پیشی فعال بعد از restart
        # DISABLED - پیشی اتوماسیون موقتاً غیرفعال است
        # try:
        #     running_pishi = await self.db.get_all_running_pishi()
        #     for cfg in running_pishi:
        #         aid = cfg['account_id']
        #         uid = cfg['user_id']
        #         task = asyncio.create_task(self._run_pishi_automation(aid, uid))
        #         self.pishi_automation_tasks[aid] = task
        # except Exception as e:
        #     logger.error(f"خطا در بازیابی اتوماسیون پیشی: {e}")

        # بازیابی پنل‌های پیشی فعال بعد از restart
        # DISABLED - پیشی پنل موقتاً غیرفعال است
        # try:
        #     running_panels = await self.db.get_all_running_pishi_panels()
        #     for panel in running_panels:
        #         uid = panel['user_id']
        #         if uid not in self.pishi_automation_tasks or \
        #            self.pishi_automation_tasks[uid].done():
        #             task = asyncio.create_task(self._run_pishi_panel(uid))
        #             self.pishi_automation_tasks[uid] = task
        # except Exception as e:
        #     logger.error(f"خطا در بازیابی پنل پیشی: {e}")
        
        # ثبت هندلرها
        self._register_handlers()
        
        # اجرای ربات
        await self.bot.run_until_disconnected()
    
    def _register_handlers(self):
        """ثبت هندلرهای ربات"""
        
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            """هندلر دستور start"""
            # ثبت کاربر
            user = await event.get_sender()
            
            # بررسی دسترسی
            is_creator = user.id in Config.ADMIN_IDS
            is_admin = await self.db.is_admin(user.id)
            
            # سازنده و ادمین‌ها خودکار approved هستن
            is_approved = is_creator or is_admin
            
            # اگر کاربر جدیده، ثبتش میکنیم
            existing_user = await self.db.get_user(user.id)
            request_sent = False  # برای جلوگیری از اسپم
            
            if not existing_user:
                await self.db.add_user(User(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    is_admin=is_admin,
                    is_approved=is_approved
                ))
                
                # اگر کاربر عادیه، فقط یکبار به سازنده اطلاع میدیم
                if not is_creator and not is_admin:
                    request_sent = True
                    for creator_id in Config.ADMIN_IDS:
                        try:
                            await self.bot.send_message(
                                creator_id,
                                f"🔔 **کاربر جدید!**\n\n"
                                f"👤 نام: {user.first_name or 'ندارد'}\n"
                                f"🆔 یوزرنیم: @{user.username or 'ندارد'}\n"
                                f"🔢 آیدی: `{user.id}`\n\n"
                                f"برای تایید دسترسی: `/approve {user.id}`",
                                buttons=[
                                    [Button.inline("✅ تایید دسترسی", f"approve_{user.id}".encode())],
                                    [Button.inline("❌ رد کردن", f"reject_{user.id}".encode())]
                                ]
                            )
                        except:
                            pass
            else:
                # بروزرسانی اطلاعات
                is_approved = existing_user.is_approved or is_creator or is_admin
                await self.db.add_user(User(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    is_admin=is_admin,
                    is_approved=is_approved
                ))
            
            await self.db.log_action('start', user.id)
            
            # اگر کاربر عادی و تایید نشده
            if not is_creator and not is_admin and not is_approved:
                if request_sent:
                    # فقط اولین بار جواب میده
                    await event.respond(
                        "⏳ **درخواست شما ارسال شد**\n\n"
                        "درخواست شما برای استفاده از ربات به سازنده ارسال شد.\n\n"
                        "لطفاً منتظر تایید باشید.\n\n"
                        "پس از تایید، به شما اطلاع داده می‌شود."
                    )
                # دفعات بعدی اصلاً جواب نمیده (ignore)
                return
            
            # منوی اصلی
            if is_creator:
                # منوی کامل برای سازنده (Creator)
                buttons = [
                    [Button.inline("➕ افزودن اکانت", b"add_account"),
                     Button.inline("📋 اکانت‌های من", b"my_accounts")],
                    [Button.inline("🔗 جوین کانال", b"join_channel"), 
                     Button.inline("🚪 لفت کانال", b"leave_channel")],
                    [Button.inline("🗑 لفت همه کانال‌ها", b"leave_all_channels")],
                    [Button.inline("🤖 استارت رفرال", b"start_referral"),
                     Button.inline("💬 ارسال پیام", b"send_message")],
                    [Button.inline("❤️ ری‌اکشن و سین", b"react_post"),
                     Button.inline("🚫 بلاک/انبلاک", b"block_user")],
                    [Button.inline("🎯 سناریو پیشرفته", b"advanced_scenario"),
                     Button.inline("👥 لیچر", b"leecher")],
                    [Button.inline("⏰ سناریو زمانبندی", b"scheduled_scenario"),
                     Button.inline("🎨 اعمال پروفایل", b"apply_profiles")],
                    [Button.inline("📝 یادداشت‌های من", b"my_notes")],
                    [Button.inline("⚙️ مدیریت ربات", b"bot_management")],
                    [Button.inline("👑 پنل ادمین", b"admin_panel")]
                ]
                
                welcome_text = (
                    "🔐 **ربات مدیریت اکانت تلگرام**\n\n"
                    "👑 **شما سازنده (Creator) هستید**\n\n"
                    "به ربات خوش آمدید! این ربات می‌تواند:\n\n"
                    "➕ **افزودن اکانت** - اضافه کردن اکانت‌های تلگرام\n"
                    "📋 **مدیریت اکانت‌ها** - مشاهده لیست اکانت‌ها\n"
                    "🔗 **جوین کانال** - عضویت در کانال/گروه\n"
                    "🚪 **لفت کانال** - خروج از کانال/گروه\n"
                    "🤖 **استارت رفرال** - استارت ربات با لینک رفرال\n"
                    "💬 **ارسال پیام** - ارسال پیام به کاربر\n"
                    "❤️ **ری‌اکشن و سین** - ری‌اکشن و سین زدن پست‌ها\n"
                    "🚫 **بلاک/انبلاک** - بلاک یا انبلاک کردن کاربر\n"
                    "🎯 **سناریو پیشرفته** - اجرای سناریوهای پیچیده\n"
                    "🐱 **پیشی** - مدیریت ربات پیشی\n\n"
                    "از منوی زیر استفاده کنید:"
                )
            elif is_admin:
                # منوی کامل برای ادمین (بدون پنل ادمین)
                buttons = [
                    [Button.inline("➕ افزودن اکانت", b"add_account"),
                     Button.inline("📋 اکانت‌های من", b"my_accounts")],
                    [Button.inline("🔗 جوین کانال", b"join_channel"), 
                     Button.inline("🚪 لفت کانال", b"leave_channel")],
                    [Button.inline("🤖 استارت رفرال", b"start_referral"),
                     Button.inline("💬 ارسال پیام", b"send_message")],
                    [Button.inline("❤️ ری‌اکشن و سین", b"react_post"),
                     Button.inline("🚫 بلاک/انبلاک", b"block_user")],
                    [Button.inline("🎯 سناریو پیشرفته", b"advanced_scenario"),
                     Button.inline("👥 لیچر", b"leecher")],
                    [Button.inline("⏰ سناریو زمانبندی", b"scheduled_scenario"),
                     Button.inline("🎨 اعمال پروفایل", b"apply_profiles")],
                    [Button.inline("📝 یادداشت‌های من", b"my_notes")],
                    [Button.inline("⚙️ مدیریت ربات", b"bot_management")],
                    [Button.inline("👑 پنل ادمین", b"admin_panel")]
                ]
                
                welcome_text = (
                    "🔐 **ربات مدیریت اکانت تلگرام**\n\n"
                    "👨‍💼 **شما ادمین هستید**\n\n"
                    "به ربات خوش آمدید! این ربات می‌تواند:\n\n"
                    "➕ **افزودن اکانت** - اضافه کردن اکانت‌های تلگرام\n"
                    "📋 **مدیریت اکانت‌ها** - مشاهده لیست اکانت‌ها\n"
                    "🔗 **جوین کانال** - عضویت در کانال/گروه\n"
                    "🚪 **لفت کانال** - خروج از کانال/گروه\n"
                    "🤖 **استارت رفرال** - استارت ربات با لینک رفرال\n"
                    "💬 **ارسال پیام** - ارسال پیام به کاربر\n"
                    "❤️ **ری‌اکشن و سین** - ری‌اکشن و سین زدن پست‌ها\n"
                    "🚫 **بلاک/انبلاک** - بلاک یا انبلاک کردن کاربر\n"
                    "🎯 **سناریو پیشرفته** - اجرای سناریوهای پیچیده\n"
                    "🐱 **پیشی** - مدیریت ربات پیشی\n"
                    "📝 **یادداشت‌ها** - ثبت یادداشت برای رباتها\n\n"
                    "از منوی زیر استفاده کنید:"
                )
            else:
                # منوی محدود برای کاربران عادی (فقط افزودن اکانت)
                buttons = [
                    [Button.inline("➕ افزودن اکانت", b"add_account")]
                ]
                
                welcome_text = (
                    "🔐 **ربات مدیریت اکانت تلگرام**\n\n"
                    "به ربات خوش آمدید!\n\n"
                    "شما می‌توانید اکانت‌های تلگرام را به ربات اضافه کنید.\n\n"
                    "➕ **افزودن اکانت** - اضافه کردن اکانت جدید\n\n"
                    "💡 اکانت‌هایی که اضافه می‌کنید برای ادمین اصلی ثبت می‌شوند.\n\n"
                    "⚠️ برای دسترسی به قابلیت‌های بیشتر، با ادمین تماس بگیرید."
                )
            
            await event.respond(welcome_text, buttons=buttons)
        
        @self.bot.on(events.CallbackQuery(pattern=b"add_account"))
        async def add_account_callback(event):
            """شروع فرآیند افزودن اکانت"""
            await event.answer()
            
            # تعیین اینکه اکانت برای کی اضافه میشه
            user_id = event.sender_id
            is_creator = user_id in Config.ADMIN_IDS
            is_admin = await self.db.is_admin(user_id)
            
            # بررسی تایید کاربر
            if not is_creator and not is_admin:
                user = await self.db.get_user(user_id)
                if not user or not user.is_approved:
                    await event.edit(
                        "⏳ **در انتظار تایید**\n\n"
                        "شما هنوز تایید نشده‌اید.\n\n"
                        "لطفاً منتظر تایید سازنده باشید.",
                        buttons=Button.inline("🔙 بازگشت", b"back_to_menu")
                    )
                    return
            
            if is_creator or is_admin:
                # ادمین‌ها برای خودشون اکانت اضافه می‌کنن
                target_user_id = user_id
                message = (
                    "📱 **افزودن اکانت**\n\n"
                    "شماره تلفن خود را ارسال کنید.\n"
                    "مثال: +989123456789\n\n"
                    "💡 این اکانت برای شما ثبت می‌شود."
                )
            else:
                # کاربران عادی برای ادمین اصلی اکانت اضافه می‌کنن
                target_user_id = Config.ADMIN_IDS[0]
                message = (
                    "📱 **افزودن اکانت**\n\n"
                    "شماره تلفن را ارسال کنید.\n"
                    "مثال: +989123456789\n\n"
                    "💡 این اکانت برای ادمین اصلی ثبت می‌شود."
                )
            
            await event.edit(
                message,
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {
                'step': 'phone',
                'target_user_id': target_user_id
            }
        
        @self.bot.on(events.CallbackQuery(pattern=b"my_accounts"))
        async def my_accounts_callback(event):
            """نمایش اکانت‌های کاربر"""
            await event.answer()
            
            user_id = event.sender_id
            is_creator = user_id in Config.ADMIN_IDS
            is_admin = await self.db.is_admin(user_id)
            
            # فقط سازنده و ادمین‌ها می‌تونن اکانت‌ها رو ببینن
            if not is_creator and not is_admin:
                await event.edit(
                    "⛔️ **دسترسی محدود**\n\n"
                    "شما فقط می‌توانید اکانت اضافه کنید.\n"
                    "برای مشاهده اکانت‌ها نیاز به دسترسی ادمین دارید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            # سازنده و ادمین‌ها فقط اکانت‌های خودشون رو می‌بینن
            accounts = await self.db.get_accounts(user_id)
            
            if is_creator:
                title = "📋 **اکانت‌های شما (سازنده):**\n\n"
            else:
                title = "📋 **اکانت‌های شما (ادمین):**\n\n"
            
            if not accounts:
                await event.edit(
                    "❌ هنوز اکانتی اضافه نشده است.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            # محدود کردن تعداد نمایش برای جلوگیری از خطای "Message too long"
            max_display = 50
            text = title
            text += f"📊 تعداد کل: {len(accounts)} اکانت\n\n"
            
            for i, acc in enumerate(accounts[:max_display], 1):
                status_emoji = "✅" if acc.status == "active" else "❌"
                text += f"{i}. {status_emoji} {acc.phone}\n"
                text += f"   👤 @{acc.telegram_username or 'ندارد'}\n"
                text += f"   📅 {acc.created_at[:10]}\n\n"
            
            if len(accounts) > max_display:
                text += f"... و {len(accounts) - max_display} اکانت دیگر\n\n"
                text += f"💡 برای مشاهده همه، از دستور /accounts استفاده کنید"
            
            await event.edit(
                text,
                buttons=[
                    [Button.inline("➕ افزودن اکانت", b"add_account")],
                    [Button.inline("🔙 بازگشت", b"back_to_menu")]
                ]
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"admin_panel"))
        async def admin_panel_callback(event):
            """پنل ادمین"""
            # فقط سازنده دسترسی داره
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            user_id = event.sender_id
            is_creator = user_id in Config.ADMIN_IDS
            
            if is_creator:
                buttons = [
                    [Button.inline("📊 آمار کلی", b"admin_stats")],
                    [Button.inline("👥 لیست کاربران", b"admin_users")],
                    [Button.inline("📱 همه اکانت‌ها", b"admin_accounts")],
                    [Button.inline("⏳ کاربران در انتظار", b"admin_pending")],
                    [Button.inline("👑 مدیریت ادمین‌ها", b"admin_manage")],
                    [Button.inline("🔐 تغییر پسورد خودکار", b"admin_auto_password")],
                    [Button.inline("📲 دریافت کد از سشن", b"admin_get_code")],
                    [Button.inline("💾 بکاپ کامل", b"admin_backup")],
                    [Button.inline("📥 ریستور بکاپ", b"admin_restore")],
                    [Button.inline("⚙️ تنظیم کانال بکاپ", b"admin_set_backup_channel")],
                    [Button.inline("🔙 بازگشت", b"back_to_menu")]
                ]
            else:
                buttons = [
                    [Button.inline("🔐 تغییر پسورد خودکار", b"admin_auto_password")],
                    [Button.inline("📲 دریافت کد از سشن", b"admin_get_code")],
                    [Button.inline("🔙 بازگشت", b"back_to_menu")]
                ]
            
            await event.edit(
                "👑 **پنل مدیریت**\n\n"
                "از منوی زیر استفاده کنید:",
                buttons=buttons
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"admin_stats"))
        async def admin_stats_callback(event):
            """نمایش آمار"""
            # فقط سازنده دسترسی داره
            if not await self._check_creator_access(event):
                return
            
            await event.answer()
            stats = await self.db.get_stats()
            
            text = "📊 **آمار کلی ربات**\n\n"
            text += f"👥 تعداد کاربران: {stats['total_users']}\n"
            text += f"📱 تعداد اکانت‌ها: {stats['total_accounts']}\n"
            text += f"✅ اکانت‌های فعال: {stats['active_accounts']}\n\n"
            
            if stats['recent_accounts']:
                text += "📋 **آخرین اکانت‌ها:**\n"
                for phone, created_at in stats['recent_accounts']:
                    text += f"• {phone} - {created_at[:10]}\n"
            
            await event.edit(
                text,
                buttons=Button.inline("🔙 بازگشت", b"admin_panel")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"admin_accounts"))
        async def admin_accounts_callback(event):
            """نمایش همه اکانت‌ها"""
            # فقط سازنده دسترسی داره
            if not await self._check_creator_access(event):
                return
            
            await event.answer()
            accounts = await self.db.get_accounts()
            
            if not accounts:
                await event.edit(
                    "❌ هنوز اکانتی ثبت نشده است.",
                    buttons=Button.inline("🔙 بازگشت", b"admin_panel")
                )
                return
            
            text = "📱 **همه اکانت‌ها:**\n\n"
            for i, acc in enumerate(accounts[:20], 1):  # نمایش 20 اکانت اول
                status_emoji = "✅" if acc.status == "active" else "❌"
                text += f"{i}. {status_emoji} {acc.phone}\n"
                text += f"   👤 @{acc.telegram_username or 'ندارد'}\n"
                text += f"   🆔 مالک: {acc.user_id}\n"
                
                # نمایش کسی که اضافه کرده
                if acc.added_by:
                    if acc.added_by == acc.user_id:
                        text += f"   ➕ توسط خودش اضافه شده\n"
                    else:
                        text += f"   ➕ اضافه شده توسط: {acc.added_by}\n"
                
                text += "\n"
            
            if len(accounts) > 20:
                text += f"... و {len(accounts) - 20} اکانت دیگر"
            
            await event.edit(
                text,
                buttons=Button.inline("🔙 بازگشت", b"admin_panel")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"admin_manage"))
        async def admin_manage_callback(event):
            """مدیریت ادمین‌ها"""
            # فقط سازنده دسترسی داره
            if not await self._check_creator_access(event):
                return
            
            await event.answer()
            
            # دریافت لیست ادمین‌ها
            admins = await self.db.get_all_admins()
            
            text = "👑 **مدیریت ادمین‌ها**\n\n"
            text += "📋 **لیست ادمین‌های فعلی:**\n\n"
            
            for admin in admins:
                name = admin.first_name or admin.username or str(admin.user_id)
                text += f"• {name} (`{admin.user_id}`)\n"
            
            text += "\n💡 **دستورات:**\n"
            text += "• برای اضافه کردن ادمین: `/addadmin USER_ID`\n"
            text += "• برای حذف ادمین: `/removeadmin USER_ID`\n\n"
            text += "مثال: `/addadmin 123456789`"
            
            await event.edit(
                text,
                buttons=Button.inline("🔙 بازگشت", b"admin_panel")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"admin_auto_password"))
        async def admin_auto_password_callback(event):
            """تنظیمات تغییر پسورد خودکار"""
            # فقط سازنده دسترسی داره
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            # دریافت وضعیت فعلی
            current_status = await self.db.get_setting('auto_change_password')
            password_mode = await self.db.get_setting('password_mode') or 'random'
            default_password = await self.db.get_setting('default_password') or 'نامشخص'
            
            is_enabled = current_status == 'enabled'
            
            status_text = "✅ فعال" if is_enabled else "❌ غیرفعال"
            status_emoji = "🟢" if is_enabled else "🔴"
            
            mode_text = "🎲 رندوم" if password_mode == 'random' else f"🔒 دیفالت (`{default_password}`)"
            
            text = (
                f"🔐 **تغییر خودکار پسورد اکانت‌ها**\n\n"
                f"وضعیت: {status_emoji} {status_text}\n"
                f"حالت پسورد: {mode_text}\n\n"
                f"📝 **توضیحات:**\n"
                f"• هنگام افزودن اکانت جدید، پسورد آن به صورت خودکار تغییر می‌کند\n"
                f"• پسورد در دیتابیس ذخیره می‌شود\n"
                f"• پسورد همراه با سشن به کانال بکاپ ارسال می‌شود\n\n"
                f"🎲 **رندوم:** هر بار پسورد جدید تولید می‌شود (10-14 کاراکتر)\n"
                f"🔒 **دیفالت:** همیشه از یک پسورد ثابت استفاده می‌شود\n\n"
            )
            
            toggle_button_text = "❌ غیرفعال کردن" if is_enabled else "✅ فعال کردن"
            toggle_button_data = b"toggle_auto_password_off" if is_enabled else b"toggle_auto_password_on"
            
            # دکمه‌های حالت با نشانگر فعال بودن
            random_btn_text = "✅ 🎲 رندوم" if password_mode == 'random' else "🎲 رندوم"
            default_btn_text = "✅ 🔒 دیفالت" if password_mode == 'default' else "🔒 دیفالت"
            
            buttons = [
                [Button.inline(toggle_button_text, toggle_button_data)],
                [
                    Button.inline(random_btn_text, b"password_mode_random"),
                    Button.inline(default_btn_text, b"password_mode_default")
                ],
                [Button.inline("✏️ تنظیم پسورد دیفالت", b"set_default_password")],
                [Button.inline("🔙 بازگشت", b"admin_panel")]
            ]
            
            await event.edit(text, buttons=buttons)
        
        @self.bot.on(events.CallbackQuery(pattern=b"toggle_auto_password_"))
        async def toggle_auto_password_callback(event):
            """تغییر وضعیت تغییر پسورد خودکار"""
            # فقط سازنده دسترسی داره
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            # تشخیص عملیات (فعال یا غیرفعال)
            action = event.data.decode().split('_')[-1]  # 'on' or 'off'
            
            if action == 'on':
                await self.db.set_setting('auto_change_password', 'enabled')
            else:
                await self.db.set_setting('auto_change_password', 'disabled')
            
            # بازگشت به منوی تنظیمات پسورد
            await admin_auto_password_callback(event)
        
        @self.bot.on(events.CallbackQuery(pattern=b"password_mode_"))
        async def password_mode_callback(event):
            """تغییر حالت پسورد (رندوم یا دیفالت)"""
            # فقط سازنده دسترسی داره
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            # تشخیص حالت
            mode = event.data.decode().split('_')[-1]  # 'random' or 'default'
            
            await self.db.set_setting('password_mode', mode)
            
            # بازگشت به منوی تنظیمات پسورد
            await admin_auto_password_callback(event)
        
        @self.bot.on(events.CallbackQuery(pattern=b"set_default_password"))
        async def set_default_password_callback(event):
            """شروع فرآیند تنظیم پسورد دیفالت"""
            # فقط سازنده دسترسی داره
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            user_id = event.sender_id
            self.user_states[user_id] = {'step': 'set_default_password'}
            
            await event.edit(
                "🔒 **تنظیم پسورد دیفالت**\n\n"
                "لطفاً پسورد دیفالت را ارسال کنید:\n\n"
                "📝 **نکات:**\n"
                "• از حروف انگلیسی (a-z, A-Z)، اعداد (0-9) و @ استفاده کنید\n"
                "• حداقل 8 کاراکتر\n"
                "• بدون فاصله\n\n"
                "مثال: `MyPass@2024`",
                buttons=Button.inline("❌ لغو", b"admin_auto_password")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"admin_get_code"))
        async def admin_get_code_callback(event):
            """شروع فرآیند دریافت کد از سشن"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id
            self.user_states[user_id] = {'step': 'get_code_phone'}
            await event.edit(
                "📲 **دریافت کد تایید از سشن**\n\n"
                "شماره اکانت را وارد کنید:\n"
                "_(مثلاً: `+989123456789`)_\n\n"
                "💡 بعد از ارسال شماره، ربات سشن رو فعال نگه می‌داره و\n"
                "به محض رسیدن کد جدید (از تلگرام) آن را نمایش می‌دهد.\n"
                "⏱ حداکثر 120 ثانیه صبر می‌کند.",
                buttons=Button.inline("🔙 بازگشت", b"admin_panel")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"get_code_wait_mode"))
        async def get_code_wait_mode_callback(event):
            """تغییر حالت به انتظار کد جدید - دیگه استفاده نمی‌شه ولی برای backward compat نگه داریم"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id
            self.user_states[user_id] = {'step': 'get_code_phone'}
            await event.edit(
                "📲 **دریافت کد تایید از سشن**\n\n"
                "شماره اکانت را وارد کنید:",
                buttons=Button.inline("🔙 بازگشت", b"admin_panel")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"admin_pending"))
        async def admin_pending_callback(event):
            """نمایش کاربران در انتظار تایید"""
            # فقط سازنده دسترسی داره
            if not await self._check_creator_access(event):
                return
            
            await event.answer()
            
            # دریافت کاربران در انتظار
            pending_users = await self.db.get_pending_users()
            
            if not pending_users:
                await event.edit(
                    "✅ **هیچ کاربری در انتظار تایید نیست!**",
                    buttons=Button.inline("🔙 بازگشت", b"admin_panel")
                )
                return
            
            text = "⏳ **کاربران در انتظار تایید:**\n\n"
            
            for i, user in enumerate(pending_users[:10], 1):
                text += f"{i}. 👤 {user.first_name or 'ندارد'}\n"
                text += f"   🆔 یوزرنیم: @{user.username or 'ندارد'}\n"
                text += f"   🔢 آیدی: `{user.user_id}`\n"
                text += f"   📅 تاریخ: {user.created_at[:10] if user.created_at else 'نامشخص'}\n\n"
            
            if len(pending_users) > 10:
                text += f"... و {len(pending_users) - 10} کاربر دیگر\n\n"
            
            text += "\n💡 **برای تایید:** `/approve USER_ID`\n"
            text += "💡 **برای رد:** `/reject USER_ID`"
            
            await event.edit(
                text,
                buttons=Button.inline("🔙 بازگشت", b"admin_panel")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"approve_"))
        async def approve_callback(event):
            """تایید دسترسی کاربر از طریق دکمه"""
            # فقط سازنده دسترسی داره
            if event.sender_id not in Config.ADMIN_IDS:
                await event.answer("⛔️ شما دسترسی ندارید!", alert=True)
                return
            
            # دریافت user_id از callback data
            user_id = int(event.data.decode().split('_')[1])
            
            # تایید کاربر
            success = await self.db.approve_user(user_id)
            
            if success:
                await event.edit(
                    f"✅ **کاربر تایید شد!**\n\n"
                    f"🆔 آیدی: `{user_id}`\n\n"
                    f"این کاربر حالا می‌تواند از ربات استفاده کند."
                )
                
                # اطلاع به کاربر
                try:
                    await self.bot.send_message(
                        user_id,
                        "✅ **دسترسی شما تایید شد!**\n\n"
                        "حالا می‌توانید از ربات استفاده کنید.\n\n"
                        "برای شروع /start را ارسال کنید."
                    )
                except:
                    pass
                
                await self.db.log_action('approve_user', event.sender_id, str(user_id))
            else:
                await event.answer("❌ خطا در تایید کاربر!", alert=True)
        
        @self.bot.on(events.CallbackQuery(pattern=b"reject_"))
        async def reject_callback(event):
            """رد دسترسی کاربر از طریق دکمه"""
            # فقط سازنده دسترسی داره
            if event.sender_id not in Config.ADMIN_IDS:
                await event.answer("⛔️ شما دسترسی ندارید!", alert=True)
                return
            
            # دریافت user_id از callback data
            user_id = int(event.data.decode().split('_')[1])
            
            await event.edit(
                f"❌ **درخواست رد شد**\n\n"
                f"🆔 آیدی: `{user_id}`\n\n"
                f"این کاربر نمی‌تواند از ربات استفاده کند."
            )
            
            # اطلاع به کاربر
            try:
                await self.bot.send_message(
                    user_id,
                    "❌ **درخواست شما رد شد**\n\n"
                    "متأسفانه نمی‌توانید از این ربات استفاده کنید."
                )
            except:
                pass
            
            await self.db.log_action('reject_user', event.sender_id, str(user_id))
        
        @self.bot.on(events.NewMessage(pattern='/approve'))
        async def approve_command_handler(event):
            """تایید دسترسی کاربر با دستور"""
            # فقط سازنده می‌تونه تایید کنه
            if event.sender_id not in Config.ADMIN_IDS:
                await event.respond("⛔️ فقط سازنده می‌تواند کاربر تایید کند!")
                return
            
            try:
                # دریافت آیدی کاربر
                parts = event.message.text.split()
                if len(parts) < 2:
                    await event.respond(
                        "❌ فرمت نادرست!\n\n"
                        "استفاده: `/approve USER_ID`\n"
                        "مثال: `/approve 123456789`"
                    )
                    return
                
                user_id = int(parts[1])
                
                # تایید کاربر
                success = await self.db.approve_user(user_id)
                
                if success:
                    await event.respond(
                        f"✅ **کاربر تایید شد!**\n\n"
                        f"🆔 آیدی: `{user_id}`\n\n"
                        f"این کاربر حالا می‌تواند از ربات استفاده کند."
                    )
                    
                    # اطلاع به کاربر
                    try:
                        await self.bot.send_message(
                            user_id,
                            "✅ **دسترسی شما تایید شد!**\n\n"
                            "حالا می‌توانید از ربات استفاده کنید.\n\n"
                            "برای شروع /start را ارسال کنید."
                        )
                    except:
                        pass
                    
                    await self.db.log_action('approve_user', event.sender_id, str(user_id))
                else:
                    await event.respond("❌ خطا در تایید کاربر!")
                    
            except ValueError:
                await event.respond("❌ آیدی نامعتبر است! لطفاً یک عدد صحیح وارد کنید.")
            except Exception as e:
                await event.respond(f"❌ خطا: {str(e)}")
        
        @self.bot.on(events.NewMessage(pattern='/reject'))
        async def reject_command_handler(event):
            """رد دسترسی کاربر با دستور"""
            # فقط سازنده می‌تونه رد کنه
            if event.sender_id not in Config.ADMIN_IDS:
                await event.respond("⛔️ فقط سازنده می‌تواند کاربر رد کند!")
                return
            
            try:
                # دریافت آیدی کاربر
                parts = event.message.text.split()
                if len(parts) < 2:
                    await event.respond(
                        "❌ فرمت نادرست!\n\n"
                        "استفاده: `/reject USER_ID`\n"
                        "مثال: `/reject 123456789`"
                    )
                    return
                
                user_id = int(parts[1])
                
                await event.respond(
                    f"❌ **درخواست رد شد**\n\n"
                    f"🆔 آیدی: `{user_id}`\n\n"
                    f"این کاربر نمی‌تواند از ربات استفاده کند."
                )
                
                # اطلاع به کاربر
                try:
                    await self.bot.send_message(
                        user_id,
                        "❌ **درخواست شما رد شد**\n\n"
                        "متأسفانه نمی‌توانید از این ربات استفاده کنید."
                    )
                except:
                    pass
                
                await self.db.log_action('reject_user', event.sender_id, str(user_id))
                    
            except ValueError:
                await event.respond("❌ آیدی نامعتبر است! لطفاً یک عدد صحیح وارد کنید.")
            except Exception as e:
                await event.respond(f"❌ خطا: {str(e)}")
        
        @self.bot.on(events.NewMessage(pattern='/addadmin'))
        async def add_admin_handler(event):
            """اضافه کردن ادمین"""
            # فقط سازنده می‌تونه ادمین اضافه کنه
            if event.sender_id not in Config.ADMIN_IDS:
                await event.respond("⛔️ فقط سازنده می‌تواند ادمین اضافه کند!")
                return
            
            try:
                # دریافت آیدی کاربر
                parts = event.message.text.split()
                if len(parts) < 2:
                    await event.respond(
                        "❌ فرمت نادرست!\n\n"
                        "استفاده: `/addadmin USER_ID`\n"
                        "مثال: `/addadmin 123456789`"
                    )
                    return
                
                new_admin_id = int(parts[1])
                
                # اضافه کردن به دیتابیس
                success = await self.db.add_admin(new_admin_id)
                
                if success:
                    await event.respond(
                        f"✅ **ادمین اضافه شد!**\n\n"
                        f"🆔 آیدی: `{new_admin_id}`\n\n"
                        f"این کاربر حالا به تمام قابلیت‌های ربات دسترسی دارد."
                    )
                    await self.db.log_action('add_admin', event.sender_id, str(new_admin_id))
                else:
                    await event.respond("❌ خطا در اضافه کردن ادمین!")
                    
            except ValueError:
                await event.respond("❌ آیدی نامعتبر است! لطفاً یک عدد صحیح وارد کنید.")
            except Exception as e:
                await event.respond(f"❌ خطا: {str(e)}")
        
        @self.bot.on(events.NewMessage(pattern='/removeadmin'))
        async def remove_admin_handler(event):
            """حذف ادمین"""
            # فقط سازنده می‌تونه ادمین حذف کنه
            if event.sender_id not in Config.ADMIN_IDS:
                await event.respond("⛔️ فقط سازنده می‌تواند ادمین حذف کند!")
                return
            
            try:
                # دریافت آیدی کاربر
                parts = event.message.text.split()
                if len(parts) < 2:
                    await event.respond(
                        "❌ فرمت نادرست!\n\n"
                        "استفاده: `/removeadmin USER_ID`\n"
                        "مثال: `/removeadmin 123456789`"
                    )
                    return
                
                admin_id = int(parts[1])
                
                # جلوگیری از حذف سازنده
                if admin_id in Config.ADMIN_IDS:
                    await event.respond("❌ نمی‌توانید سازنده را حذف کنید!")
                    return
                
                # حذف از دیتابیس
                success = await self.db.remove_admin(admin_id)
                
                if success:
                    await event.respond(
                        f"✅ **ادمین حذف شد!**\n\n"
                        f"🆔 آیدی: `{admin_id}`\n\n"
                        f"این کاربر دیگر دسترسی ادمین ندارد."
                    )
                    await self.db.log_action('remove_admin', event.sender_id, str(admin_id))
                else:
                    await event.respond("❌ خطا در حذف ادمین!")
                    
            except ValueError:
                await event.respond("❌ آیدی نامعتبر است! لطفاً یک عدد صحیح وارد کنید.")
            except Exception as e:
                await event.respond(f"❌ خطا: {str(e)}")
        
        @self.bot.on(events.CallbackQuery(pattern=b"join_channel"))
        async def join_channel_callback(event):
            """شروع فرآیند جوین کانال"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            accounts = await self.db.get_accounts(event.sender_id)
            
            if not accounts:
                await event.edit(
                    "❌ شما هنوز اکانتی اضافه نکرده‌اید.\n"
                    "ابتدا یک اکانت اضافه کنید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            await event.edit(
                "🔗 **جوین کانال/گروه**\n\n"
                "لینک کانال یا گروه را ارسال کنید:\n\n"
                "✅ لینک عمومی: https://t.me/channel\n"
                "✅ لینک خصوصی: https://t.me/+hash\n"
                "✅ یوزرنیم: @channel یا channel",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'join_link'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"leave_channel"))
        async def leave_channel_callback(event):
            """شروع فرآیند لفت کانال"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            accounts = await self.db.get_accounts(event.sender_id)
            
            if not accounts:
                await event.edit(
                    "❌ شما هنوز اکانتی اضافه نکرده‌اید.\n"
                    "ابتدا یک اکانت اضافه کنید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            await event.edit(
                "🚪 **لفت کانال/گروه**\n\n"
                "لینک یا یوزرنیم کانال/گروه را ارسال کنید:\n\n"
                "✅ لینک: https://t.me/channel\n"
                "✅ یوزرنیم: @channel یا channel",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'leave_link'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"leave_all_channels"))
        async def leave_all_channels_callback(event):
            """شروع فرآیند لفت از همه کانال‌ها"""
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            user_id = event.sender_id
            accounts = await self.db.get_accounts(user_id)
            active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
            
            if not active_accounts:
                await event.edit(
                    "❌ شما اکانت فعالی ندارید.",
                    buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                )
                return
            
            self.user_states[user_id] = {
                'step': 'scenario_count',
                'active_accounts': active_accounts,
                'operation_type': 'leave_all_channels',
                'selected_country': None,
                'scenario_summary': "🗑 **عملیات: لفت همه کانال‌ها و گروه‌ها**"
            }
            
            await event.edit(
                f"🗑 **لفت همه کانال‌ها و گروه‌ها**\n\n"
                f"⚠️ هر اکانت از **تمام** کانال‌ها و گروه‌هایی که عضوشونه خارج می‌شود.\n"
                f"(چت‌های خصوصی دست‌نخورده می‌مانند)\n\n"
                f"📊 اکانت‌های فعال: {len(active_accounts)}\n\n"
                f"💡 **گزینه‌ها:**\n"
                f"• عدد بفرست (مثلاً `5`)\n"
                f"• `/all` - همه اکانت‌ها\n"
                f"• `/from 70` - از اکانت 70\n"
                f"• `/from 70 to 100` - از 70 تا 100",
                buttons=Button.inline("❌ لغو", b"cancel")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_menu"))
        async def pishi_menu_callback(event):
            """پنل اتوماسیون پیشی"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id

            # ── موقتاً غیرفعال ──────────────────────────────────────
            if user_id not in Config.ADMIN_IDS:
                await event.edit(
                    "🐱 **پنل پیشی**\n\n"
                    "🚧 این بخش در حال توسعه است و به زودی فعال می‌شود.",
                    buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                )
                return
            # ────────────────────────────────────────────────────────
            cfg = await self.db.get_pishi_panel(user_id)
            if not cfg:
                await self.db.init_pishi_panel(user_id)
                cfg = await self.db.get_pishi_panel(user_id)

            accounts = await self.db.get_accounts(user_id)
            active_accounts = [a for a in accounts if a.status == 'active' and a.session_path]
            total_acc = len(active_accounts)
            acc_count = cfg.get('account_count') or total_acc
            if acc_count == 'all' or acc_count == total_acc:
                acc_count_text = f"همه ({total_acc})"
            else:
                acc_count_text = f"{acc_count} از {total_acc}"

            is_run = user_id in self.pishi_automation_tasks and \
                     not self.pishi_automation_tasks[user_id].done()
            run_icon = "🟢 روشن" if is_run else "🔴 خاموش"
            mio_icon   = "✅" if cfg['mio_enabled']      else "❌"
            fish_icon  = "✅" if cfg['fish_enabled']     else "❌"
            trans_icon = "✅" if cfg['transfer_enabled'] else "❌"
            target_text = f"msg #{cfg['transfer_target_msg_id']}" \
                          if cfg.get('transfer_target_msg_id') else "تنظیم نشده"

            text = (
                f"🐱 **پنل اتوماسیون پیشی**\n\n"
                f"وضعیت: {run_icon}\n"
                f"📢 گروه: `{cfg['group_username']}`\n"
                f"👥 اکانت: {acc_count_text}\n\n"
                f"{mio_icon} **میو** — هر {cfg['mio_interval_minutes']} دقیقه\n"
                f"{fish_icon} **ماهی** — هر {cfg['fish_interval_minutes']} دقیقه\n"
                f"{trans_icon} **انتقال اتومات** — ساعت {cfg.get('transfer_hour',0):02d}:00\n"
                f"   🎯 target: {target_text}\n"
            )
            run_btn = Button.inline("⏹ خاموش کردن", b"pishi_stop_panel") if is_run else \
                      Button.inline("▶️ روشن کردن", b"pishi_start_panel")
            buttons = [
                [run_btn],
                [Button.inline(f"{mio_icon} میو", b"pishi_toggle_mio_p"),
                 Button.inline("⏱ زمان میو", b"pishi_set_mio_p")],
                [Button.inline(f"{fish_icon} ماهی", b"pishi_toggle_fish_p"),
                 Button.inline("⏱ زمان ماهی", b"pishi_set_fish_p")],
                [Button.inline(f"{trans_icon} انتقال اتومات", b"pishi_toggle_trans_p"),
                 Button.inline("⚙️ تنظیمات انتقال", b"pishi_set_trans_p")],
                [Button.inline("👥 تعداد اکانت", b"pishi_set_count_p")],
                [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
            ]
            await event.edit(text, buttons=buttons)

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_automation"))
        async def pishi_automation_redirect(event):
            """redirect به پنل اصلی"""
            if not await self._check_admin_access(event): return
            await event.answer()
            await pishi_menu_callback(event)

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_start_panel"))
        async def pishi_start_panel(event):
            if not await self._check_admin_access(event): return
            await event.answer()
            user_id = event.sender_id
            await self.db.upsert_pishi_panel(user_id, is_running=1)
            if user_id not in self.pishi_automation_tasks or \
               self.pishi_automation_tasks[user_id].done():
                task = asyncio.create_task(self._run_pishi_panel(user_id))
                self.pishi_automation_tasks[user_id] = task
            await pishi_menu_callback(event)

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_stop_panel"))
        async def pishi_stop_panel(event):
            if not await self._check_admin_access(event): return
            await event.answer()
            user_id = event.sender_id
            await self.db.upsert_pishi_panel(user_id, is_running=0)
            task = self.pishi_automation_tasks.pop(user_id, None)
            if task: task.cancel()
            await pishi_menu_callback(event)

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_toggle_mio_p"))
        async def pishi_toggle_mio_p(event):
            if not await self._check_admin_access(event): return
            await event.answer()
            cfg = await self.db.get_pishi_panel(event.sender_id)
            await self.db.upsert_pishi_panel(event.sender_id, mio_enabled=0 if cfg['mio_enabled'] else 1)
            await pishi_menu_callback(event)

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_toggle_fish_p"))
        async def pishi_toggle_fish_p(event):
            if not await self._check_admin_access(event): return
            await event.answer()
            cfg = await self.db.get_pishi_panel(event.sender_id)
            await self.db.upsert_pishi_panel(event.sender_id, fish_enabled=0 if cfg['fish_enabled'] else 1)
            await pishi_menu_callback(event)

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_toggle_trans_p"))
        async def pishi_toggle_trans_p(event):
            if not await self._check_admin_access(event): return
            await event.answer()
            user_id = event.sender_id
            cfg = await self.db.get_pishi_panel(user_id)
            if not cfg['transfer_enabled'] and not cfg.get('transfer_target_msg_id'):
                await event.answer("❌ ابتدا تنظیمات انتقال را کامل کنید!", alert=True)
                return
            await self.db.upsert_pishi_panel(user_id, transfer_enabled=0 if cfg['transfer_enabled'] else 1)
            await pishi_menu_callback(event)

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_set_mio_p"))
        async def pishi_set_mio_p(event):
            if not await self._check_admin_access(event): return
            await event.answer()
            self.user_states[event.sender_id] = {'step': 'pishi_p_mio_interval'}
            await event.edit(
                "⏱ **زمانبندی میو**\n\nهر چند دقیقه میو بزنه؟\nمثال: `5`",
                buttons=Button.inline("❌ لغو", b"pishi_menu")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_set_fish_p"))
        async def pishi_set_fish_p(event):
            if not await self._check_admin_access(event): return
            await event.answer()
            self.user_states[event.sender_id] = {'step': 'pishi_p_fish_interval'}
            await event.edit(
                "⏱ **زمانبندی ماهی**\n\nهر چند دقیقه ماهی بزنه؟\nمثال: `60`",
                buttons=Button.inline("❌ لغو", b"pishi_menu")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_set_trans_p"))
        async def pishi_set_trans_p(event):
            if not await self._check_admin_access(event): return
            await event.answer()
            user_id = event.sender_id
            cfg = await self.db.get_pishi_panel(user_id)
            target = f"msg #{cfg['transfer_target_msg_id']}" if cfg and cfg.get('transfer_target_msg_id') else "تنظیم نشده"
            hour = cfg.get('transfer_hour', 0) if cfg else 0
            self.user_states[user_id] = {'step': 'pishi_p_trans_settings'}
            await event.edit(
                f"⚙️ **تنظیمات انتقال اتومات**\n\n"
                f"🕐 ساعت فعلی: {hour:02d}:00\n"
                f"🎯 target فعلی: {target}\n\n"
                f"یکی از موارد زیر را ارسال کنید:\n"
                f"• لینک پیام target: `https://t.me/meavmeacv/113309`\n"
                f"• ساعت انتقال: `hour:0` (نیمه‌شب) یا `hour:23`",
                buttons=Button.inline("❌ لغو", b"pishi_menu")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_set_count_p"))
        async def pishi_set_count_p(event):
            if not await self._check_admin_access(event): return
            await event.answer()
            user_id = event.sender_id
            accounts = await self.db.get_accounts(user_id)
            total = len([a for a in accounts if a.status == 'active' and a.session_path])
            self.user_states[user_id] = {'step': 'pishi_p_account_count'}
            await event.edit(
                f"👥 **تعداد اکانت**\n\nاکانت‌های فعال: {total}\n\n"
                f"چند تا استفاده شوند؟\n• عدد (مثلاً `10`) یا `/all`",
                buttons=Button.inline("❌ لغو", b"pishi_menu")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_transfer"))
        async def pishi_transfer_callback(event):
            """انتقال موجودی"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id

            accounts = await self.db.get_accounts(user_id)
            active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]

            if not active_accounts:
                await event.edit(
                    "❌ اکانت فعالی ندارید.",
                    buttons=Button.inline("🔙 برگشت", b"pishi_menu")
                )
                return

            self.user_states[user_id] = {
                'step': 'pishi_transfer_group',
                'active_accounts': active_accounts,
            }
            await event.edit(
                "💰 **انتقال موجودی میویی — مرحله ۱/۳**\n\n"
                "آیدی یا لینک گروه را ارسال کنید:\n\n"
                "مثال: `@meavmeacv` یا `https://t.me/meavmeacv`",
                buttons=Button.inline("❌ لغو", b"cancel")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_fish"))
        async def pishi_fish_callback(event):
            """ماهی"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            await event.edit(
                "🐟 **ماهی**\n\n"
                "🚧 این قابلیت به زودی اضافه می‌شود.",
                buttons=Button.inline("🔙 برگشت", b"pishi_menu")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_empty"))
        async def pishi_empty_callback(event):
            """خالی کردن پیشی"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            await event.edit(
                "🗑 **خالی کردن پیشی**\n\n"
                "🚧 این قابلیت به زودی اضافه می‌شود.\n\n"
                "برای تعریف این قابلیت با سازنده صحبت کنید.",
                buttons=Button.inline("🔙 برگشت", b"pishi_menu")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"pishi_upgrade"))
        async def pishi_upgrade_callback(event):
            """ارتقا دادن پیشی"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            await event.edit(
                "⬆️ **ارتقا دادن پیشی**\n\n"
                "🚧 این قابلیت به زودی اضافه می‌شود.\n\n"
                "برای تعریف این قابلیت با سازنده صحبت کنید.",
                buttons=Button.inline("🔙 برگشت", b"pishi_menu")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"start_referral"))
        async def start_referral_callback(event):
            """شروع فرآیند استارت رفرال"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            accounts = await self.db.get_accounts(event.sender_id)
            
            if not accounts:
                await event.edit(
                    "❌ شما هنوز اکانتی اضافه نکرده‌اید.\n"
                    "ابتدا یک اکانت اضافه کنید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            await event.edit(
                "🤖 **استارت ربات با رفرال**\n\n"
                "لینک رفرال ربات را ارسال کنید:\n\n"
                "✅ فرمت 1: https://t.me/bot_name?start=ref_id\n"
                "✅ فرمت 2: @bot_name ref_id\n\n"
                "مثال:\n"
                "https://t.me/amxvpn_bot?start=631388884\n"
                "یا\n"
                "@amxvpn_bot 631388884",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'referral_link'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"send_message"))
        async def send_message_callback(event):
            """شروع فرآیند ارسال پیام"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            accounts = await self.db.get_accounts(event.sender_id)
            
            if not accounts:
                await event.edit(
                    "❌ شما هنوز اکانتی اضافه نکرده‌اید.\n"
                    "ابتدا یک اکانت اضافه کنید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            await event.edit(
                "💬 **ارسال پیام**\n\n"
                "یوزرنیم یا آیدی عددی کاربر مقصد را ارسال کنید:\n\n"
                "✅ یوزرنیم: @username یا username\n"
                "✅ آیدی عددی: 123456789\n\n"
                "مثال:\n"
                "@john_doe\n"
                "یا\n"
                "631388884",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'message_target'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"react_post"))
        async def react_post_callback(event):
            """منوی ری‌اکشن و سین"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            accounts = await self.db.get_accounts(event.sender_id)
            
            if not accounts:
                await event.edit(
                    "❌ شما هنوز اکانتی اضافه نکرده‌اید.\n"
                    "ابتدا یک اکانت اضافه کنید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            await event.edit(
                "❤️ **ری‌اکشن و سین زدن پست**\n\n"
                "چه کاری میخواهید انجام دهید؟",
                buttons=[
                    [Button.inline("❤️ ری‌اکشن + سین", b"do_react_and_view")],
                    [Button.inline("👁 فقط سین", b"do_view_only")],
                    [Button.inline("🔙 بازگشت", b"back_to_menu")]
                ]
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"do_react_and_view"))
        async def do_react_and_view_callback(event):
            """شروع فرآیند ری‌اکشن و سین"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            await event.edit(
                "❤️ **ری‌اکشن و سین زدن پست**\n\n"
                "لینک پست کانال را ارسال کنید:\n\n"
                "✅ فرمت: https://t.me/channel/123\n"
                "✅ یا: https://t.me/c/1234567890/123\n\n"
                "💡 **نکات:**\n"
                "• هر اکانت یک ری‌اکشن تصادفی می‌زند\n"
                "• سین (view) پست هم زده می‌شود\n"
                "• همه اکانت‌ها این کار را انجام می‌دهند\n\n"
                "مثال:\n"
                "https://t.me/mychannel/456",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'react_link'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"do_view_only"))
        async def do_view_only_callback(event):
            """شروع فرآیند فقط سین"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            await event.edit(
                "👁 **فقط سین زدن پست**\n\n"
                "لینک پست کانال را ارسال کنید:\n\n"
                "✅ فرمت: https://t.me/channel/123\n"
                "✅ یا: https://t.me/c/1234567890/123\n\n"
                "💡 **نکات:**\n"
                "• فقط سین (view) پست زده می‌شود\n"
                "• هیچ ری‌اکشنی ارسال نمی‌شود\n"
                "• همه اکانت‌ها این کار را انجام می‌دهند\n\n"
                "مثال:\n"
                "https://t.me/mychannel/456",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'view_only_link'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"block_user"))
        async def block_user_callback(event):
            """منوی بلاک/انبلاک"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            accounts = await self.db.get_accounts(event.sender_id)
            
            if not accounts:
                await event.edit(
                    "❌ شما هنوز اکانتی اضافه نکرده‌اید.\n"
                    "ابتدا یک اکانت اضافه کنید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            await event.edit(
                "🚫 **بلاک/انبلاک کاربر**\n\n"
                "چه کاری میخواهید انجام دهید؟",
                buttons=[
                    [Button.inline("🚫 بلاک کردن", b"do_block")],
                    [Button.inline("✅ انبلاک کردن", b"do_unblock")],
                    [Button.inline("🔙 بازگشت", b"back_to_menu")]
                ]
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"do_block"))
        async def do_block_callback(event):
            """شروع فرآیند بلاک"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            await event.edit(
                "🚫 **بلاک کردن کاربر**\n\n"
                "یوزرنیم یا آیدی عددی کاربر را ارسال کنید:\n\n"
                "✅ یوزرنیم: @username یا username\n"
                "✅ آیدی عددی: 123456789\n\n"
                "💡 **نکته:** این کاربر توسط همه اکانت‌های شما بلاک می‌شود.\n\n"
                "مثال:\n"
                "@spammer\n"
                "یا\n"
                "123456789",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'block_target'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"do_unblock"))
        async def do_unblock_callback(event):
            """شروع فرآیند انبلاک"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            await event.edit(
                "✅ **انبلاک کردن کاربر**\n\n"
                "یوزرنیم یا آیدی عددی کاربر را ارسال کنید:\n\n"
                "✅ یوزرنیم: @username یا username\n"
                "✅ آیدی عددی: 123456789\n\n"
                "💡 **نکته:** این کاربر توسط همه اکانت‌های شما انبلاک می‌شود.\n\n"
                "مثال:\n"
                "@someone\n"
                "یا\n"
                "123456789",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'unblock_target'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"bot_management"))
        async def bot_management_callback(event):
            """منوی مدیریت ربات"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            # دریافت آمار
            accounts = await self.db.get_accounts(event.sender_id)
            active_count = len([acc for acc in accounts if acc.status == 'active'])
            
            buttons = [
                [Button.inline("🔄 تنظیمات تایمر", b"timer_settings")],
                [Button.inline("📊 آمار من", b"my_stats")],
                [Button.inline("❓ راهنما", b"help")],
                [Button.inline("🔙 بازگشت", b"back_to_menu")]
            ]
            
            await event.edit(
                f"⚙️ **مدیریت ربات**\n\n"
                f"📱 تعداد اکانت‌ها: {len(accounts)}\n"
                f"✅ اکانت‌های فعال: {active_count}\n"
                f"⏱ تاخیر فعلی: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                f"از منوی زیر استفاده کنید:",
                buttons=buttons
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"timer_settings"))
        async def timer_settings_callback(event):
            """تنظیمات تایمر"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            await event.edit(
                f"⏱ **تنظیمات تایمر**\n\n"
                f"تاخیر فعلی: {Config.DELAY_BETWEEN_ACTIONS} ثانیه\n"
                f"محدوده تصادفی: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                f"💡 **توضیحات:**\n"
                f"• تاخیر بین هر عملیات برای جلوگیری از فلود\n"
                f"• محدوده تصادفی برای طبیعی‌تر بودن\n"
                f"• تنظیمات فعلی ایمن و بهینه است\n\n"
                f"⚠️ برای تغییر تنظیمات، فایل .env را ویرایش کنید:\n"
                f"DELAY_BETWEEN_ACTIONS=5\n"
                f"DELAY_RANDOM_RANGE=3",
                buttons=Button.inline("🔙 بازگشت", b"bot_management")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"my_stats"))
        async def my_stats_callback(event):
            """آمار کاربر"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            # دریافت آمار از دیتابیس
            accounts = await self.db.get_accounts(event.sender_id)
            
            active_count = len([acc for acc in accounts if acc.status == 'active'])
            inactive_count = len(accounts) - active_count
            
            # آخرین فعالیت‌ها
            stats_text = f"📊 **آمار شما**\n\n"
            stats_text += f"📱 کل اکانت‌ها: {len(accounts)}\n"
            stats_text += f"✅ فعال: {active_count}\n"
            stats_text += f"❌ غیرفعال: {inactive_count}\n\n"
            
            if accounts:
                stats_text += "📋 **آخرین اکانت‌ها:**\n"
                for acc in accounts[:5]:
                    status = "✅" if acc.status == "active" else "❌"
                    stats_text += f"{status} {acc.phone} - {acc.created_at[:10]}\n"
            
            await event.edit(
                stats_text,
                buttons=Button.inline("🔙 بازگشت", b"bot_management")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"help"))
        async def help_callback(event):
            """راهنما"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            help_text = (
                "❓ **راهنمای استفاده**\n\n"
                "**➕ افزودن اکانت:**\n"
                "شماره → کد تایید → رمز (اختیاری)\n\n"
                "**🔗 جوین کانال:**\n"
                "لینک کانال/گروه → جوین خودکار\n\n"
                "**🚪 لفت کانال:**\n"
                "یوزرنیم کانال → لفت خودکار\n\n"
                "**🤖 استارت رفرال:**\n"
                "لینک رفرال → استارت خودکار\n\n"
                "**💬 ارسال پیام:**\n"
                "یوزرنیم/آیدی → متن پیام → ارسال خودکار\n\n"
                "**❤️ ری‌اکشن و سین:**\n"
                "• ری‌اکشن + سین: لینک پست → ری‌اکشن تصادفی + سین\n"
                "• فقط سین: لینک پست → فقط سین (بدون ری‌اکشن)\n\n"
                "**🚫 بلاک/انبلاک:**\n"
                "یوزرنیم/آیدی → بلاک یا انبلاک خودکار\n\n"
                "**🎯 سناریو پیشرفته:**\n"
                "سناریوی کامل برای تعامل با ربات‌ها\n"
                "• دستورات: start, send, click, solve_captcha, share_phone, join, leave, auto_join, wait, stop, forward\n"
                "• کلیک دکمه: با متن یا شماره (click: #0, click: 1)\n"
                "• حل کپچا: solve_captcha: send یا solve_captcha: click یا solve_captcha: send, 3\n"
                "• اشتراک شماره: share_phone: (خودکار شماره اکانت رو میفرسته)\n"
                "• جوین خودکار: auto_join (جوین همه کانال‌های پیام + کلیک آخرین دکمه)\n"
                "• توقف موقت: stop: 5 (5 ثانیه توقف)\n"
                "• فوروارد نتایج: forward: 5, @mychannel\n"
                "• متغیرهای دینامیک: {random:N}, {random_upper:N}, {random_num:N}\n"
                "• پشتیبانی از چند ربات در یک سناریو\n"
                "• گزارش کامل در فایل .txt\n\n"
                "💡 **نکات:**\n"
                "• همه عملیات با تایمر و تاخیر انجام می‌شود\n"
                "• برای جلوگیری از بن، تنظیمات را تغییر ندهید\n"
                "• اکانت‌های خود را به صورت دوره‌ای چک کنید\n"
                "• از متغیرهای دینامیک برای یونیک بودن استفاده کنید\n"
                "• از شماره دکمه وقتی متن دکمه‌ها متفاوت است\n"
                "• از forward برای جمع‌آوری نتایج در یک کانال\n"
                "• از stop برای توقف موقت بین مراحل"
            )
            
            await event.edit(
                help_text,
                buttons=Button.inline("🔙 بازگشت", b"bot_management")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"cancel|back_to_menu"))
        async def cancel_callback(event):
            """لغو عملیات"""
            await event.answer()
            
            user_id = event.sender_id
            
            # لغو عملیات در حال اجرا (اگر وجود داشته باشد)
            if user_id in self.running_operations:
                self.running_operations[user_id]['cancelled'] = True
                # صبر کمی تا task پس‌زمینه متوجه لغو بشه
                await asyncio.sleep(0.5)
                # حذف flag
                if user_id in self.running_operations:
                    del self.running_operations[user_id]
            
            # لغو فرآیند ورود اگر در حال انجام است
            if user_id in self.user_states:
                await self.receiver.cancel_login(user_id)
                del self.user_states[user_id]
            
            # بازگشت به منوی اصلی
            user = await event.get_sender()
            
            # بررسی دسترسی کاربر
            is_creator = user.id in Config.ADMIN_IDS
            is_admin = await self.db.is_admin(user.id)
            
            if is_creator:
                # منوی کامل برای سازنده
                buttons = [
                    [Button.inline("➕ افزودن اکانت", b"add_account"),
                     Button.inline("📋 اکانت‌های من", b"my_accounts")],
                    [Button.inline("🔗 جوین کانال", b"join_channel"), 
                     Button.inline("🚪 لفت کانال", b"leave_channel")],
                    [Button.inline("🗑 لفت همه کانال‌ها", b"leave_all_channels")],
                    [Button.inline("🤖 استارت رفرال", b"start_referral"),
                     Button.inline("💬 ارسال پیام", b"send_message")],
                    [Button.inline("❤️ ری‌اکشن و سین", b"react_post"),
                     Button.inline("🚫 بلاک/انبلاک", b"block_user")],
                    [Button.inline("🎯 سناریو پیشرفته", b"advanced_scenario"),
                     Button.inline("👥 لیچر", b"leecher")],
                    [Button.inline("⏰ سناریو زمانبندی", b"scheduled_scenario"),
                     Button.inline("🎨 اعمال پروفایل", b"apply_profiles")],
                    [Button.inline("📝 یادداشت‌های من", b"my_notes")],
                    [Button.inline("⚙️ مدیریت ربات", b"bot_management")],
                    [Button.inline("👑 پنل ادمین", b"admin_panel")]
                ]
            elif is_admin:
                # منوی کامل برای ادمین (بدون پنل ادمین)
                buttons = [
                    [Button.inline("➕ افزودن اکانت", b"add_account"),
                     Button.inline("📋 اکانت‌های من", b"my_accounts")],
                    [Button.inline("🔗 جوین کانال", b"join_channel"), 
                     Button.inline("🚪 لفت کانال", b"leave_channel")],
                    [Button.inline("🗑 لفت همه کانال‌ها", b"leave_all_channels")],
                    [Button.inline("🤖 استارت رفرال", b"start_referral"),
                     Button.inline("💬 ارسال پیام", b"send_message")],
                    [Button.inline("❤️ ری‌اکشن و سین", b"react_post"),
                     Button.inline("🚫 بلاک/انبلاک", b"block_user")],
                    [Button.inline("🎯 سناریو پیشرفته", b"advanced_scenario"),
                     Button.inline("👥 لیچر", b"leecher")],
                    [Button.inline("⏰ سناریو زمانبندی", b"scheduled_scenario"),
                     Button.inline("🎨 اعمال پروفایل", b"apply_profiles")],
                    [Button.inline("📝 یادداشت‌های من", b"my_notes")],
                    [Button.inline("⚙️ مدیریت ربات", b"bot_management")],
                    [Button.inline("👑 پنل ادمین", b"admin_panel")]
                ]
            else:
                # منوی محدود برای کاربران عادی (فقط افزودن اکانت)
                buttons = [
                    [Button.inline("➕ افزودن اکانت", b"add_account")]
                ]
            
            await event.edit(
                "🔐 **منوی اصلی**",
                buttons=buttons
            )
        
        @self.bot.on(events.NewMessage(pattern='/cancel'))
        async def cancel_handler(event):
            """هندلر لغو عملیات"""
            if event.sender_id in self.user_states:
                await self.receiver.cancel_login(event.sender_id)
                del self.user_states[event.sender_id]
            await event.respond("❌ عملیات لغو شد.")
        
        @self.bot.on(events.NewMessage(func=lambda e: (not e.message.text.startswith('/')) or 
                                                      (e.message.text.lower() in ['/all']) or 
                                                      (e.message.text.lower().startswith('/from '))))
        async def message_handler(event):
            """هندلر پیام‌های عادی و دستورات خاص"""
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                return
            
            state = self.user_states[user_id]
            step = state.get('step')
            
            if step == 'phone':
                # دریافت شماره تلفن
                phone = event.message.text.strip()
                
                # ارسال درخواست کد
                await event.respond("⏳ در حال ارسال کد تایید...")
                
                result = await self.receiver.send_code_request(phone, user_id)
                
                if result['success']:
                    state['phone'] = phone
                    state['step'] = 'code'
                    await event.respond(
                        f"✅ کد تایید به شماره `{phone}` ارسال شد.\n\n"
                        "لطفاً کد 5 رقمی را ارسال کنید:",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    await self.db.log_action('code_sent', user_id, phone)
                else:
                    await event.respond(
                        f"❌ خطا: {result['message']}",
                        buttons=[
                            [Button.inline("🔄 تلاش مجدد", b"add_account")],
                            [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                        ]
                    )
                    del self.user_states[user_id]
            
            elif step == 'code':
                # دریافت کد تایید
                code_text = event.message.text.strip()
                
                # استخراج کد از متن (اگر کل پیام تلگرام رو کپی کرده)
                code = extract_telegram_code(code_text)
                
                if not code:
                    # اگر استخراج نشد، خود متن رو به عنوان کد در نظر بگیر
                    code = code_text
                
                await event.respond("⏳ در حال بررسی کد...")
                
                result = await self.receiver.sign_in_with_code(
                    user_id=user_id,
                    phone=state['phone'],
                    code=code
                )
                
                if result.get('need_restart'):
                    # نیاز به شروع مجدد
                    await event.respond(
                        f"❌ {result['message']}",
                        buttons=[
                            [Button.inline("🔄 شروع مجدد", b"add_account")],
                            [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                        ]
                    )
                    del self.user_states[user_id]
                
                elif result.get('need_password'):
                    # نیاز به رمز دو مرحله‌ای
                    state['step'] = 'password'
                    await event.respond(
                        "🔐 **رمز دو مرحله‌ای مورد نیاز است**\n\n"
                        "لطفاً رمز عبور خود را ارسال کنید:",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                
                elif result.get('completed'):
                    # ورود موفق
                    target_user_id = state.get('target_user_id', user_id)
                    
                    # تشخیص کد کشور از روی شماره
                    from src.utils.countries import detect_country_from_phone
                    country_code = detect_country_from_phone(state['phone'])
                    
                    # بررسی تنظیمات تغییر خودکار پسورد
                    auto_change_password = await self.db.get_setting('auto_change_password')
                    new_password = None
                    password_changed = False
                    
                    if auto_change_password == 'enabled':
                        try:
                            # دریافت حالت پسورد (رندوم یا دیفالت)
                            password_mode = await self.db.get_setting('password_mode') or 'random'
                            
                            if password_mode == 'default':
                                # استفاده از پسورد دیفالت
                                new_password = await self.db.get_setting('default_password')
                                
                                if not new_password:
                                    # اگر پسورد دیفالت تنظیم نشده، از رندوم استفاده می‌کنیم
                                    logger.warning("پسورد دیفالت تنظیم نشده، از حالت رندوم استفاده می‌شود")
                                    password_mode = 'random'
                            
                            if password_mode == 'random':
                                # تولید پسورد قوی و ایمن (فقط حروف و اعداد - قابل قبول توسط تلگرام)
                                password_length = random.randint(10, 14)
                                # فقط حروف بزرگ، کوچک و اعداد (بدون کاراکترهای خاص)
                                new_password = ''.join(random.choices(
                                    string.ascii_letters + string.digits, 
                                    k=password_length
                                ))
                            
                            # ذخیره پسورد قبل از تغییر (برای بازیابی در صورت خطا)
                            temp_password = new_password
                            
                            # تغییر پسورد اکانت
                            await event.respond(f"🔐 در حال تغییر پسورد اکانت... (حالت: {password_mode})")
                            password_result = await self.receiver.change_account_password(
                                result['session_path'],
                                new_password
                            )
                            
                            if password_result['success']:
                                password_changed = True
                                logger.info(f"پسورد اکانت {state['phone']} با موفقیت تغییر کرد (حالت: {password_mode})")
                            else:
                                # اگر تغییر پسورد موفق نبود، پسورد رو null می‌ذاریم
                                new_password = None
                                
                                # بررسی اینکه آیا اکانت قبلاً پسورد داشته یا نه
                                if password_result.get('has_password'):
                                    logger.info(f"اکانت {state['phone']} قبلاً پسورد دارد، پسورد تغییر نمی‌کند")
                                    # فقط به ادمین اطلاع می‌دیم
                                    if user_id in Config.ADMIN_IDS or user_id == target_user_id:
                                        await event.respond(
                                            f"ℹ️ این اکانت قبلاً پسورد دارد.\n"
                                            f"اکانت بدون تغییر پسورد ذخیره می‌شود."
                                        )
                                else:
                                    logger.warning(f"تغییر پسورد ناموفق: {password_result['message']}")
                                    # فقط به ادمین اطلاع می‌دیم
                                    if user_id in Config.ADMIN_IDS or user_id == target_user_id:
                                        await event.respond(
                                            f"⚠️ تغییر پسورد ناموفق بود: {password_result['message']}\n"
                                            f"اکانت بدون تغییر پسورد ذخیره می‌شود."
                                        )
                        except Exception as e:
                            # در صورت هر خطایی، پسورد رو null می‌ذاریم
                            new_password = None
                            logger.exception(f"خطا در فرآیند تغییر پسورد: {e}")
                            if user_id in Config.ADMIN_IDS or user_id == target_user_id:
                                await event.respond(
                                    f"⚠️ خطا در تغییر پسورد: {str(e)}\n"
                                    f"اکانت بدون تغییر پسورد ذخیره می‌شود."
                                )
                    
                    # بررسی تکراری نبودن اکانت (ورود با کد)
                    existing = await self.db.get_account_by_phone(state['phone'])
                    if existing:
                        import os as _os
                        try:
                            _os.remove(result['session_path'])
                        except:
                            pass
                        state['step'] = 'phone'
                        await event.respond(
                            f"⚠️ **این اکانت قبلاً ثبت شده است!**\n\n"
                            f"📱 شماره: {state['phone']}\n"
                            f"🆔 یوزرنیم: @{existing.telegram_username or 'ندارد'}\n"
                            f"📅 تاریخ ثبت: {existing.created_at[:10] if existing.created_at else 'نامشخص'}\n\n"
                            f"📱 برای افزودن اکانت دیگری شماره تلفن را ارسال کنید.\n"
                            f"یا /cancel برای بازگشت به منوی اصلی.",
                            buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                        )
                        return
                    
                    # ذخیره اکانت در دیتابیس (حتی اگر تغییر پسورد ناموفق باشد)
                    try:
                        account = Account(
                            user_id=target_user_id,  # ذخیره برای کاربر هدف
                            phone=state['phone'],
                            telegram_user_id=result['user_id'],
                            telegram_username=result.get('username'),
                            session_path=result['session_path'],
                            status='active',
                            added_by=user_id,  # کسی که اکانت رو اضافه کرده
                            country_code=country_code if country_code != 'UNKNOWN' else None,
                            password=new_password  # پسورد جدید (اگر تغییر داده شده باشد)
                        )
                        await self.db.add_account(account)
                        await self.db.log_action('account_added', user_id, f"{state['phone']} -> user:{target_user_id} (password_changed: {password_changed})")
                        logger.info(f"اکانت {state['phone']} با موفقیت در دیتابیس ذخیره شد")
                    except Exception as e:
                        logger.exception(f"خطا در ذخیره اکانت در دیتابیس: {e}")
                        await event.respond(
                            f"❌ خطا در ذخیره اکانت: {str(e)}\n"
                            f"لطفاً با پشتیبانی تماس بگیرید."
                        )
                        return
                    
                    # آپلود سشن به کانال بکاپ (اگر تنظیم شده باشد)
                    if self.backup_manager.backup_channel_id:
                        try:
                            asyncio.create_task(
                                self.backup_manager.upload_session_to_channel(
                                    result['session_path'],
                                    state['phone'],
                                    result.get('username'),
                                    new_password  # ارسال پسورد به کانال بکاپ
                                )
                            )
                            logger.info(f"سشن {state['phone']} برای آپلود به کانال بکاپ ارسال شد")
                        except Exception as e:
                            logger.exception(f"خطا در ارسال سشن به کانال بکاپ: {e}")
                    
                    # بازگشت به مرحله دریافت شماره برای اکانت بعدی
                    state['step'] = 'phone'
                    
                    # پیام متفاوت برای ادمین و کاربر عادی
                    # فقط به ادمین پسورد نشون داده می‌شه
                    is_admin_user = user_id in Config.ADMIN_IDS
                    password_info = ""
                    
                    if new_password and is_admin_user:
                        password_info = f"\n🔐 پسورد جدید: `{new_password}`"
                    
                    if target_user_id == user_id:
                        # کاربر برای خودش اکانت اضافه کرده
                        if is_admin_user:
                            success_msg = (
                                f"✅ **ورود موفق!**\n\n"
                                f"👤 نام: {result.get('first_name', 'نامشخص')}\n"
                                f"🆔 یوزرنیم: @{result.get('username') or 'ندارد'}\n"
                                f"📱 شماره: {state['phone']}\n"
                                f"📁 سشن ذخیره شد{password_info}\n\n"
                                f"✨ **اکانت شما با موفقیت ثبت شد!**\n\n"
                                f"📱 برای افزودن اکانت بعدی، شماره تلفن را ارسال کنید.\n"
                                f"یا /cancel برای بازگشت به منوی اصلی."
                            )
                        else:
                            # کاربر عادی - بدون نمایش پسورد
                            success_msg = (
                                f"✅ **ورود موفق!**\n\n"
                                f"👤 نام: {result.get('first_name', 'نامشخص')}\n"
                                f"🆔 یوزرنیم: @{result.get('username') or 'ندارد'}\n"
                                f"📱 شماره: {state['phone']}\n"
                                f"📁 سشن ذخیره شد\n\n"
                                f"✨ **اکانت شما با موفقیت ثبت شد!**\n\n"
                                f"📱 برای افزودن اکانت بعدی، شماره تلفن را ارسال کنید.\n"
                                f"یا /cancel برای بازگشت به منوی اصلی."
                            )
                    else:
                        # کاربر عادی برای ادمین اکانت اضافه کرده
                        success_msg = (
                            f"✅ **ورود موفق!**\n\n"
                            f"👤 نام: {result.get('first_name', 'نامشخص')}\n"
                            f"🆔 یوزرنیم: @{result.get('username') or 'ندارد'}\n"
                            f"📱 شماره: {state['phone']}\n"
                            f"📁 سشن ذخیره شد\n\n"
                            f"✨ **اکانت برای ادمین اصلی ثبت شد!**\n\n"
                            f"📱 برای افزودن اکانت بعدی، شماره تلفن را ارسال کنید.\n"
                            f"یا /cancel برای بازگشت به منوی اصلی."
                        )
                    
                    await event.respond(
                        success_msg,
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                
                else:
                    # خطا
                    await event.respond(
                        f"❌ {result['message']}",
                        buttons=[
                            [Button.inline("🔄 تلاش مجدد", b"add_account")],
                            [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                        ]
                    )
                    await self.db.log_action('login_failed', user_id, result['message'])
            
            elif step == 'password':
                # دریافت رمز دو مرحله‌ای
                password = event.message.text.strip()
                
                # ذخیره پسورد فعلی در state برای استفاده بعدی (تغییر پسورد)
                state['current_password'] = password
                
                await event.respond("⏳ در حال بررسی رمز...")
                
                result = await self.receiver.sign_in_with_password(
                    user_id=user_id,
                    password=password
                )
                
                if result.get('need_restart'):
                    # نیاز به شروع مجدد
                    await event.respond(
                        f"❌ {result['message']}",
                        buttons=[
                            [Button.inline("🔄 شروع مجدد", b"add_account")],
                            [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                        ]
                    )
                    del self.user_states[user_id]
                
                elif result.get('completed'):
                    # ورود موفق
                    target_user_id = state.get('target_user_id', user_id)
                    
                    # تشخیص کد کشور از روی شماره
                    from src.utils.countries import detect_country_from_phone
                    phone = state.get('phone', 'unknown')
                    country_code = detect_country_from_phone(phone) if phone != 'unknown' else None
                    
                    # بررسی تنظیمات تغییر خودکار پسورد
                    auto_change_password = await self.db.get_setting('auto_change_password')
                    new_password = None
                    password_changed = False
                    
                    if auto_change_password == 'enabled':
                        try:
                            # دریافت حالت پسورد (رندوم یا دیفالت)
                            password_mode = await self.db.get_setting('password_mode') or 'random'
                            
                            if password_mode == 'default':
                                # استفاده از پسورد دیفالت
                                new_password = await self.db.get_setting('default_password')
                                
                                if not new_password:
                                    logger.warning("پسورد دیفالت تنظیم نشده، از حالت رندوم استفاده می‌شود")
                                    password_mode = 'random'
                            
                            if password_mode == 'random':
                                password_length = random.randint(10, 14)
                                new_password = ''.join(random.choices(
                                    string.ascii_letters + string.digits, 
                                    k=password_length
                                ))
                            
                            # پسورد فعلی اکانت (که کاربر موقع لاگین وارد کرده)
                            current_account_password = state.get('current_password')
                            
                            # تغییر پسورد اکانت
                            password_result = await self.receiver.change_account_password(
                                result['session_path'],
                                new_password,
                                current_password=current_account_password  # پسورد فعلی برای اکانت‌هایی که پسورد دارن
                            )
                            
                            if password_result['success']:
                                password_changed = True
                                logger.info(f"پسورد اکانت {phone} با موفقیت تغییر کرد (حالت: {password_mode})")
                            else:
                                new_password = None
                                if password_result.get('has_password'):
                                    logger.info(f"اکانت {phone} قبلاً پسورد دارد، پسورد تغییر نمی‌کند")
                                    if user_id in Config.ADMIN_IDS or user_id == target_user_id:
                                        await event.respond(
                                            f"ℹ️ این اکانت قبلاً پسورد دارد.\n"
                                            f"اکانت بدون تغییر پسورد ذخیره می‌شود."
                                        )
                                else:
                                    logger.warning(f"تغییر پسورد ناموفق: {password_result['message']}")
                                    if user_id in Config.ADMIN_IDS or user_id == target_user_id:
                                        await event.respond(
                                            f"⚠️ تغییر پسورد ناموفق بود: {password_result['message']}\n"
                                            f"اکانت بدون تغییر پسورد ذخیره می‌شود."
                                        )
                        except Exception as e:
                            new_password = None
                            logger.exception(f"خطا در فرآیند تغییر پسورد: {e}")
                            if user_id in Config.ADMIN_IDS or user_id == target_user_id:
                                await event.respond(
                                    f"⚠️ خطا در تغییر پسورد: {str(e)}\n"
                                    f"اکانت بدون تغییر پسورد ذخیره می‌شود."
                                )
                    
                    # بررسی تکراری نبودن اکانت (ورود با پسورد 2FA)
                    existing = await self.db.get_account_by_phone(phone)
                    if existing:
                        import os as _os
                        try:
                            _os.remove(result['session_path'])
                        except:
                            pass
                        state['step'] = 'phone'
                        await event.respond(
                            f"⚠️ **این اکانت قبلاً ثبت شده است!**\n\n"
                            f"📱 شماره: {phone}\n"
                            f"🆔 یوزرنیم: @{existing.telegram_username or 'ندارد'}\n"
                            f"📅 تاریخ ثبت: {existing.created_at[:10] if existing.created_at else 'نامشخص'}\n\n"
                            f"📱 برای افزودن اکانت دیگری شماره تلفن را ارسال کنید.\n"
                            f"یا /cancel برای بازگشت به منوی اصلی.",
                            buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                        )
                        return
                    
                    # ذخیره اکانت در دیتابیس (حتی اگر تغییر پسورد ناموفق باشد)
                    try:
                        account = Account(
                            user_id=target_user_id,  # ذخیره برای کاربر هدف
                            phone=phone,
                            telegram_user_id=result['user_id'],
                            telegram_username=result.get('username'),
                            session_path=result['session_path'],
                            status='active',
                            added_by=user_id,  # کسی که اکانت رو اضافه کرده
                            country_code=country_code if country_code and country_code != 'UNKNOWN' else None,
                            password=new_password  # پسورد جدید (اگر تغییر داده شده باشد)
                        )
                        await self.db.add_account(account)
                        await self.db.log_action('account_added', user_id, f"{phone} -> user:{target_user_id} (password_changed: {password_changed})")
                        logger.info(f"اکانت {phone} با موفقیت در دیتابیس ذخیره شد")
                    except Exception as e:
                        logger.exception(f"خطا در ذخیره اکانت در دیتابیس: {e}")
                        await event.respond(
                            f"❌ خطا در ذخیره اکانت: {str(e)}\n"
                            f"لطفاً با پشتیبانی تماس بگیرید."
                        )
                        return
                    
                    # آپلود سشن به کانال بکاپ (اگر تنظیم شده باشد)
                    if self.backup_manager.backup_channel_id:
                        try:
                            asyncio.create_task(
                                self.backup_manager.upload_session_to_channel(
                                    result['session_path'],
                                    state.get('phone', 'unknown'),
                                    result.get('username'),
                                    new_password  # ارسال پسورد به کانال بکاپ
                                )
                            )
                            logger.info(f"سشن {phone} برای آپلود به کانال بکاپ ارسال شد")
                        except Exception as e:
                            logger.exception(f"خطا در ارسال سشن به کانال بکاپ: {e}")
                    
                    # بازگشت به مرحله دریافت شماره برای اکانت بعدی
                    state['step'] = 'phone'
                    
                    # پیام متفاوت برای ادمین و کاربر عادی
                    # فقط به ادمین پسورد نشون داده می‌شه
                    is_admin_user = user_id in Config.ADMIN_IDS
                    password_info = ""
                    
                    if new_password and is_admin_user:
                        password_info = f"\n🔐 پسورد جدید: `{new_password}`"
                    
                    if target_user_id == user_id:
                        # کاربر برای خودش اکانت اضافه کرده
                        if is_admin_user:
                            success_msg = (
                                f"✅ **ورود موفق!**\n\n"
                                f"👤 نام: {result.get('first_name', 'نامشخص')}\n"
                                f"🆔 یوزرنیم: @{result.get('username') or 'ندارد'}\n"
                                f"📁 سشن ذخیره شد{password_info}\n\n"
                                f"✨ **اکانت شما با موفقیت ثبت شد!**\n\n"
                                f"📱 برای افزودن اکانت بعدی، شماره تلفن را ارسال کنید.\n"
                                f"یا /cancel برای بازگشت به منوی اصلی."
                            )
                        else:
                            # کاربر عادی - بدون نمایش پسورد
                            success_msg = (
                                f"✅ **ورود موفق!**\n\n"
                                f"👤 نام: {result.get('first_name', 'نامشخص')}\n"
                                f"🆔 یوزرنیم: @{result.get('username') or 'ندارد'}\n"
                                f"📁 سشن ذخیره شد\n\n"
                                f"✨ **اکانت شما با موفقیت ثبت شد!**\n\n"
                                f"📱 برای افزودن اکانت بعدی، شماره تلفن را ارسال کنید.\n"
                                f"یا /cancel برای بازگشت به منوی اصلی."
                            )
                    else:
                        # کاربر عادی برای ادمین اکانت اضافه کرده
                        success_msg = (
                            f"✅ **ورود موفق!**\n\n"
                            f"👤 نام: {result.get('first_name', 'نامشخص')}\n"
                            f"🆔 یوزرنیم: @{result.get('username') or 'ندارد'}\n"
                            f"📁 سشن ذخیره شد\n\n"
                            f"✨ **اکانت برای ادمین اصلی ثبت شد!**\n\n"
                            f"📱 برای افزودن اکانت بعدی، شماره تلفن را ارسال کنید.\n"
                            f"یا /cancel برای بازگشت به منوی اصلی."
                        )
                    
                    await event.respond(
                        success_msg,
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                
                else:
                    # خطا
                    await event.respond(
                        f"❌ {result['message']}\n\n"
                        "لطفاً رمز صحیح را وارد کنید:",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    await self.db.log_action('password_failed', user_id, result['message'])

            elif step == 'set_default_password':
                # دریافت پسورد دیفالت از ادمین
                password_text = event.message.text.strip()
                
                # اعتبارسنجی پسورد
                import re
                
                # حروف انگلیسی، اعداد و @ مجاز هستند
                if not re.match(r'^[a-zA-Z0-9@]+$', password_text):
                    await event.respond(
                        "❌ **پسورد نامعتبر!**\n\n"
                        "فقط از حروف انگلیسی (a-z, A-Z)، اعداد (0-9) و @ استفاده کنید.\n"
                        "بدون فاصله و کاراکترهای خاص دیگر.\n\n"
                        "لطفاً دوباره تلاش کنید:",
                        buttons=Button.inline("❌ لغو", b"admin_auto_password")
                    )
                    return
                
                # حداقل 8 کاراکتر
                if len(password_text) < 8:
                    await event.respond(
                        "❌ **پسورد خیلی کوتاه است!**\n\n"
                        "پسورد باید حداقل 8 کاراکتر باشد.\n\n"
                        "لطفاً دوباره تلاش کنید:",
                        buttons=Button.inline("❌ لغو", b"admin_auto_password")
                    )
                    return
                
                # حداکثر 32 کاراکتر
                if len(password_text) > 32:
                    await event.respond(
                        "❌ **پسورد خیلی بلند است!**\n\n"
                        "پسورد باید حداکثر 32 کاراکتر باشد.\n\n"
                        "لطفاً دوباره تلاش کنید:",
                        buttons=Button.inline("❌ لغو", b"admin_auto_password")
                    )
                    return
                
                # ذخیره پسورد دیفالت
                await self.db.set_setting('default_password', password_text)
                
                # پاک کردن state
                del self.user_states[user_id]
                
                await event.respond(
                    f"✅ **پسورد دیفالت تنظیم شد!**\n\n"
                    f"🔒 پسورد: `{password_text}`\n\n"
                    f"از این به بعد، اگر حالت دیفالت فعال باشد، این پسورد برای همه اکانت‌های جدید استفاده می‌شود.",
                    buttons=Button.inline("🔙 بازگشت به تنظیمات", b"admin_auto_password")
                )
                
                logger.info(f"پسورد دیفالت توسط ادمین {user_id} تنظیم شد")

            elif step == 'get_code_phone':
                # دریافت شماره برای خواندن کد از سشن
                phone = event.message.text.strip()
                
                # نرمال‌سازی شماره
                if not phone.startswith('+'):
                    phone = '+' + phone
                
                # پیدا کردن اکانت در دیتابیس
                account = await self.db.get_account_by_phone(phone)
                
                if not account:
                    await event.respond(
                        f"❌ اکانت با شماره `{phone}` در دیتابیس پیدا نشد.",
                        buttons=[
                            [Button.inline("🔄 تلاش مجدد", b"admin_get_code")],
                            [Button.inline("🔙 بازگشت", b"admin_panel")]
                        ]
                    )
                    return
                
                if not account.session_path:
                    await event.respond(
                        f"❌ اکانت `{phone}` سشن ندارد.",
                        buttons=Button.inline("🔙 بازگشت", b"admin_panel")
                    )
                    return
                
                del self.user_states[user_id]
                
                # cancel flag برای لغو عملیات
                cancel_flag = {'cancelled': False}
                operation_key = f'get_code_{user_id}'
                self.running_operations[operation_key] = cancel_flag
                
                progress_msg = await event.respond(
                    f"📲 **منتظر کد جدید...**\n\n"
                    f"📱 شماره: `{phone}`\n"
                    f"⏱ حداکثر 120 ثانیه صبر می‌کنم.\n\n"
                    f"💡 الان از گوشی یا دستگاه دیگه‌ای وارد اون اکانت بشید تا کد بیاد.",
                    buttons=Button.inline("❌ لغو", f"cancel_get_code_{user_id}".encode())
                )
                
                # دریافت کد جدید با cancel_flag
                from src.services.code_reader import get_code_from_session
                result = await get_code_from_session(
                    account.session_path,
                    timeout=120,
                    cancel_flag=cancel_flag
                )
                
                # پاک کردن operation
                self.running_operations.pop(operation_key, None)
                
                if cancel_flag.get('cancelled'):
                    return  # پیام قبلاً توسط cancel handler آپدیت شده
                
                if result['success']:
                    await progress_msg.edit(
                        f"✅ **کد دریافت شد!**\n\n"
                        f"📱 شماره: `{phone}`\n"
                        f"🔑 کد: `{result['code']}`\n\n"
                        f"💬 متن پیام:\n`{result.get('msg_preview', '')[:100]}`",
                        buttons=[
                            [Button.inline("🔄 دریافت کد جدید", b"admin_get_code")],
                            [Button.inline("🔙 بازگشت", b"admin_panel")]
                        ]
                    )
                    logger.info(f"کد اکانت {phone} توسط ادمین {user_id} دریافت شد: {result['code']}")
                else:
                    await progress_msg.edit(
                        f"⏰ **کدی دریافت نشد**\n\n"
                        f"📱 شماره: `{phone}`\n"
                        f"📝 {result['message']}",
                        buttons=[
                            [Button.inline("🔄 تلاش مجدد", b"admin_get_code")],
                            [Button.inline("🔙 بازگشت", b"admin_panel")]
                        ]
                    )

            elif step == 'join_link':
                # دریافت لینک برای جوین
                channel_link = event.message.text.strip()
                
                # دریافت اکانت‌های کاربر
                accounts = await self.db.get_accounts(user_id)
                active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                
                if not active_accounts:
                    await event.respond(
                        "❌ شما اکانت فعالی ندارید.",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                    del self.user_states[user_id]
                    return
                
                # ذخیره اطلاعات و انتقال به فرآیند رفرال/کشور پیشرفته
                state['channel_link'] = channel_link
                state['active_accounts'] = active_accounts
                state['operation_type'] = 'join_channel'
                state['scenario_summary'] = f"🔗 **عملیات: جوین کانال/گروه**\n📢 کانال: {channel_link}"
                
                # نمایش دکمه‌های انتخاب کشور
                countries = await self.db.get_countries(user_id)
                if len(countries) > 1:
                    state['step'] = 'scenario_country'
                    country_text, buttons = self._create_country_buttons(countries)
                    await event.respond(country_text, buttons=buttons)
                else:
                    state['selected_country'] = None
                    state['step'] = 'scenario_count'
                    await event.respond(
                        f"📊 **انتخاب تعداد اکانت**\n\n"
                        f"{state['scenario_summary']}\n\n"
                        f"شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                        f"چند تا اکانت برای جوین کانال استفاده شود؟\n\n"
                        f"💡 **گزینه‌ها:**\n"
                        f"• عدد بفرست (مثلاً `5`)\n"
                        f"• `/all` - همه اکانت‌ها\n"
                        f"• `/from 70` - از اکانت 70\n"
                        f"• `/from 70 to 100` - از 70 تا 100",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
            
            elif step == 'join_count':
                # دریافت تعداد اکانت
                count_input = event.message.text.strip()
                
                active_accounts = state['active_accounts']
                channel_link = state['channel_link']
                
                # تعیین تعداد اکانت
                if count_input.lower() == '/all':
                    selected_accounts = active_accounts
                else:
                    try:
                        count = int(count_input)
                        if count <= 0:
                            await event.respond(
                                "❌ تعداد باید بیشتر از صفر باشد!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return
                        selected_accounts = active_accounts[:min(count, len(active_accounts))]
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً یک عدد معتبر یا /all ارسال کنید.",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                total = len(selected_accounts)
                
                # ارسال پیام شروع
                progress_msg = await event.respond(
                    f"⏳ **شروع عملیات جوین**\n\n"
                    f"📊 تعداد اکانت‌ها: {total}\n"
                    f"⏱ تاخیر بین هر عملیات: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                    f"لطفاً صبر کنید..."
                )
                
                # تابع callback برای بروزرسانی پیشرفت
                async def update_progress(current, total, message):
                    try:
                        await progress_msg.edit(
                            f"⏳ **در حال جوین...**\n\n"
                            f"📊 پیشرفت: {current}/{total}\n"
                            f"💬 {message}"
                        )
                    except:
                        pass
                
                # جوین دسته‌جمعی با تایمر
                session_paths = [acc.session_path for acc in selected_accounts]
                results = await self.channel_manager.bulk_join(
                    session_paths,
                    channel_link,
                    progress_callback=update_progress
                )
                
                # نمایش نتایج
                results_text = "📊 **نتایج جوین:**\n\n"
                
                for i, detail in enumerate(results['details'][:10], 1):  # نمایش 10 مورد اول
                    phone_short = selected_accounts[i-1].phone[-4:] if selected_accounts[i-1].phone else "****"
                    result = detail['result']
                    
                    if result['success']:
                        results_text += f"✅ {phone_short}: موفق\n"
                    else:
                        results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                
                if len(results['details']) > 10:
                    results_text += f"\n... و {len(results['details']) - 10} مورد دیگر\n"
                
                results_text += f"\n✅ موفق: {results['success']}\n"
                results_text += f"❌ ناموفق: {results['failed']}"
                
                # بررسی سشن‌های نامعتبر
                invalid_count = await self._process_bulk_results_for_invalid_sessions(results, selected_accounts)
                if invalid_count > 0:
                    results_text += f"\n⚠️ سشن نامعتبر: {invalid_count} (غیرفعال شد)"
                
                await progress_msg.edit(
                    results_text,
                    buttons=[
                        [Button.inline("🔗 جوین مجدد", b"join_channel")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
                
                await self.db.log_action('bulk_join', user_id, f"{channel_link} - {results['success']}/{total}")
                del self.user_states[user_id]
            
            elif step == 'leave_link':
                # دریافت لینک برای لفت
                channel_link = event.message.text.strip()
                
                # دریافت اکانت‌های کاربر
                accounts = await self.db.get_accounts(user_id)
                active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                
                if not active_accounts:
                    await event.respond(
                        "❌ شما اکانت فعالی ندارید.",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                    del self.user_states[user_id]
                    return
                
                # ذخیره اطلاعات و انتقال به فرآیند رفرال/کشور پیشرفته
                state['channel_link'] = channel_link
                state['active_accounts'] = active_accounts
                state['operation_type'] = 'leave_channel'
                state['scenario_summary'] = f"🚪 **عملیات: لفت کانال/گروه**\n📢 کانال: {channel_link}"
                
                # نمایش دکمه‌های انتخاب کشور
                countries = await self.db.get_countries(user_id)
                if len(countries) > 1:
                    state['step'] = 'scenario_country'
                    country_text, buttons = self._create_country_buttons(countries)
                    await event.respond(country_text, buttons=buttons)
                else:
                    state['selected_country'] = None
                    state['step'] = 'scenario_count'
                    await event.respond(
                        f"📊 **انتخاب تعداد اکانت**\n\n"
                        f"{state['scenario_summary']}\n\n"
                        f"شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                        f"چند تا اکانت برای لفت استفاده شود؟\n\n"
                        f"💡 **گزینه‌ها:**\n"
                        f"• عدد بفرست (مثلاً `5`)\n"
                        f"• `/all` - همه اکانت‌ها\n"
                        f"• `/from 70` - از اکانت 70\n"
                        f"• `/from 70 to 100` - از 70 تا 100",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
            
            elif step == 'leave_count':
                # دریافت تعداد اکانت
                count_input = event.message.text.strip()
                
                active_accounts = state['active_accounts']
                channel_link = state['channel_link']
                
                # تعیین تعداد اکانت
                if count_input.lower() == '/all':
                    selected_accounts = active_accounts
                else:
                    try:
                        count = int(count_input)
                        if count <= 0:
                            await event.respond(
                                "❌ تعداد باید بیشتر از صفر باشد!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return
                        selected_accounts = active_accounts[:min(count, len(active_accounts))]
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً یک عدد معتبر یا /all ارسال کنید.",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                total = len(selected_accounts)
                
                # ارسال پیام شروع
                progress_msg = await event.respond(
                    f"⏳ **شروع عملیات لفت**\n\n"
                    f"📊 تعداد اکانت‌ها: {total}\n"
                    f"⏱ تاخیر بین هر عملیات: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                    f"لطفاً صبر کنید..."
                )
                
                # تابع callback برای بروزرسانی پیشرفت
                async def update_progress(current, total, message):
                    try:
                        await progress_msg.edit(
                            f"⏳ **در حال لفت...**\n\n"
                            f"📊 پیشرفت: {current}/{total}\n"
                            f"💬 {message}"
                        )
                    except:
                        pass
                
                # لفت دسته‌جمعی با تایمر
                session_paths = [acc.session_path for acc in selected_accounts]
                results = await self.channel_manager.bulk_leave(
                    session_paths,
                    channel_link,
                    progress_callback=update_progress
                )
                
                # نمایش نتایج
                results_text = "📊 **نتایج لفت:**\n\n"
                
                for i, detail in enumerate(results['details'][:10], 1):  # نمایش 10 مورد اول
                    phone_short = selected_accounts[i-1].phone[-4:] if selected_accounts[i-1].phone else "****"
                    result = detail['result']
                    
                    if result['success']:
                        results_text += f"✅ {phone_short}: موفق\n"
                    else:
                        results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                
                if len(results['details']) > 10:
                    results_text += f"\n... و {len(results['details']) - 10} مورد دیگر\n"
                
                results_text += f"\n✅ موفق: {results['success']}\n"
                results_text += f"❌ ناموفق: {results['failed']}"
                
                invalid_count = await self._process_bulk_results_for_invalid_sessions(results, selected_accounts)
                if invalid_count > 0:
                    results_text += f"\n⚠️ سشن نامعتبر: {invalid_count} (غیرفعال شد)"
                
                await progress_msg.edit(
                    results_text,
                    buttons=[
                        [Button.inline("🚪 لفت مجدد", b"leave_channel")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
                
                await self.db.log_action('bulk_leave', user_id, f"{channel_link} - {results['success']}/{total}")
                del self.user_states[user_id]

            elif step == 'referral_link':
                # دریافت لینک رفرال
                referral_input = event.message.text.strip()
                
                # تجزیه لینک رفرال
                parsed = self.referral_manager.parse_referral_link(referral_input)
                
                if 'error' in parsed:
                    await event.respond(
                        f"❌ {parsed['error']}\n\n"
                        "لطفاً لینک را به فرمت صحیح ارسال کنید:\n"
                        "https://t.me/bot_name?start=ref_id\n"
                        "یا\n"
                        "@bot_name ref_id",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    return
                
                bot_username = parsed['bot_username']
                start_param = parsed['start_param']
                
                # دریافت اکانت‌های کاربر
                accounts = await self.db.get_accounts(user_id)
                active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                
                if not active_accounts:
                    await event.respond(
                        "❌ شما اکانت فعالی ندارید.",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                    del self.user_states[user_id]
                    return
                
                # ذخیره اطلاعات و انتقال به فرآیند رفرال/کشور پیشرفته
                state['bot_username'] = bot_username
                state['start_param'] = start_param
                state['active_accounts'] = active_accounts
                state['operation_type'] = 'start_referral'
                state['scenario_summary'] = f"🤖 **عملیات: استارت رفرال**\n🤖 ربات: @{bot_username}\n🔑 پارامتر: {start_param}"
                
                # نمایش دکمه‌های انتخاب کشور
                countries = await self.db.get_countries(user_id)
                if len(countries) > 1:
                    state['step'] = 'scenario_country'
                    country_text, buttons = self._create_country_buttons(countries)
                    await event.respond(country_text, buttons=buttons)
                else:
                    state['selected_country'] = None
                    state['step'] = 'scenario_count'
                    await event.respond(
                        f"📊 **انتخاب تعداد اکانت**\n\n"
                        f"{state['scenario_summary']}\n\n"
                        f"شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                        f"چند تا اکانت برای استارت رفرال استفاده شود؟\n\n"
                        f"💡 **گزینه‌ها:**\n"
                        f"• عدد بفرست (مثلاً `5`)\n"
                        f"• `/all` - همه اکانت‌ها\n"
                        f"• `/from 70` - از اکانت 70\n"
                        f"• `/from 70 to 100` - از 70 تا 100",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
            
            elif step == 'referral_count':
                # دریافت تعداد اکانت
                count_input = event.message.text.strip()
                
                active_accounts = state['active_accounts']
                bot_username = state['bot_username']
                start_param = state['start_param']
                
                # تعیین تعداد اکانت
                if count_input.lower() == '/all':
                    selected_accounts = active_accounts
                else:
                    try:
                        count = int(count_input)
                        if count <= 0:
                            await event.respond(
                                "❌ تعداد باید بیشتر از صفر باشد!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return
                        selected_accounts = active_accounts[:min(count, len(active_accounts))]
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً یک عدد معتبر یا /all ارسال کنید.",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                total = len(selected_accounts)
                
                # ارسال پیام شروع
                progress_msg = await event.respond(
                    f"⏳ **شروع عملیات استارت رفرال**\n\n"
                    f"🤖 ربات: @{bot_username}\n"
                    f"🔗 رفرال: {start_param}\n"
                    f"📊 تعداد اکانت‌ها: {total}\n"
                    f"⏱ تاخیر بین هر عملیات: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                    f"لطفاً صبر کنید..."
                )
                
                # تابع callback برای بروزرسانی پیشرفت
                async def update_progress(current, total, message):
                    try:
                        await progress_msg.edit(
                            f"⏳ **در حال استارت...**\n\n"
                            f"🤖 ربات: @{bot_username}\n"
                            f"📊 پیشرفت: {current}/{total}\n"
                            f"💬 {message}"
                        )
                    except:
                        pass
                
                # استارت دسته‌جمعی با تایمر (بدون کلیک دکمه)
                session_paths = [acc.session_path for acc in selected_accounts]
                results = await self.referral_manager.bulk_start_bot(
                    session_paths,
                    bot_username,
                    start_param,
                    click_button=None,
                    progress_callback=update_progress
                )
                
                # نمایش نتایج
                results_text = "📊 **نتایج استارت رفرال:**\n\n"
                results_text += f"🤖 ربات: @{bot_username}\n"
                results_text += f"🔗 رفرال: {start_param}\n\n"
                
                for i, detail in enumerate(results['details'][:10], 1):  # نمایش 10 مورد اول
                    phone_short = selected_accounts[i-1].phone[-4:] if selected_accounts[i-1].phone else "****"
                    result = detail['result']
                    
                    if result['success']:
                        results_text += f"✅ {phone_short}: موفق\n"
                    else:
                        results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                
                if len(results['details']) > 10:
                    results_text += f"\n... و {len(results['details']) - 10} مورد دیگر\n"
                
                results_text += f"\n✅ موفق: {results['success']}\n"
                results_text += f"❌ ناموفق: {results['failed']}"
                
                await progress_msg.edit(
                    results_text,
                    buttons=[
                        [Button.inline("🤖 استارت مجدد", b"start_referral")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
                
                await self.db.log_action('bulk_referral', user_id, f"@{bot_username} {start_param} - {results['success']}/{total}")
                del self.user_states[user_id]

            elif step == 'message_target':
                # دریافت یوزرنیم یا آیدی مقصد
                target = event.message.text.strip()
                
                state['target'] = target
                state['step'] = 'message_text'
                
                await event.respond(
                    f"💬 **ارسال پیام به: {target}**\n\n"
                    "حالا متن پیام خود را ارسال کنید:",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )
            
            elif step == 'message_text':
                # دریافت متن پیام
                message_text = event.message.text.strip()
                target = state['target']
                
                # دریافت اکانت‌های کاربر
                accounts = await self.db.get_accounts(user_id)
                active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                
                if not active_accounts:
                    await event.respond(
                        "❌ شما اکانت فعالی ندارید.",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                    del self.user_states[user_id]
                    return
                
                # ذخیره اطلاعات و پرسیدن تعداد اکانت
                state['message_text'] = message_text
                state['active_accounts'] = active_accounts
                state['step'] = 'message_count'
                
                await event.respond(
                    f"📊 **انتخاب تعداد اکانت**\n\n"
                    f"شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                    f"چند تا اکانت برای ارسال پیام استفاده شود؟\n\n"
                    f"💡 عدد ارسال کنید (مثلاً 5) یا:\n"
                    f"• /all برای همه اکانت‌ها",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )
            
            elif step == 'message_count':
                # دریافت تعداد اکانت
                count_input = event.message.text.strip()
                
                active_accounts = state['active_accounts']
                target = state['target']
                message_text = state['message_text']
                
                # تعیین تعداد اکانت
                if count_input.lower() == '/all':
                    selected_accounts = active_accounts
                else:
                    try:
                        count = int(count_input)
                        if count <= 0:
                            await event.respond(
                                "❌ تعداد باید بیشتر از صفر باشد!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return
                        selected_accounts = active_accounts[:min(count, len(active_accounts))]
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً یک عدد معتبر یا /all ارسال کنید.",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                total = len(selected_accounts)
                
                # نمایش پیش‌نمایش پیام
                preview_text = message_text[:100] + "..." if len(message_text) > 100 else message_text
                
                # ارسال پیام شروع
                progress_msg = await event.respond(
                    f"⏳ **شروع عملیات ارسال پیام**\n\n"
                    f"👤 مقصد: {target}\n"
                    f"💬 پیام: {preview_text}\n"
                    f"📊 تعداد اکانت‌ها: {total}\n"
                    f"⏱ تاخیر بین هر عملیات: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                    f"لطفاً صبر کنید..."
                )
                
                # تابع callback برای بروزرسانی پیشرفت
                async def update_progress(current, total, message):
                    try:
                        await progress_msg.edit(
                            f"⏳ **در حال ارسال...**\n\n"
                            f"👤 مقصد: {target}\n"
                            f"📊 پیشرفت: {current}/{total}\n"
                            f"💬 {message}"
                        )
                    except:
                        pass
                
                # ارسال دسته‌جمعی با تایمر
                session_paths = [acc.session_path for acc in selected_accounts]
                results = await self.message_sender.bulk_send_message(
                    session_paths,
                    target,
                    message_text,
                    progress_callback=update_progress
                )
                
                # نمایش نتایج
                results_text = "📊 **نتایج ارسال پیام:**\n\n"
                results_text += f"👤 مقصد: {target}\n"
                results_text += f"💬 پیام: {preview_text}\n\n"
                
                for i, detail in enumerate(results['details'][:10], 1):  # نمایش 10 مورد اول
                    phone_short = selected_accounts[i-1].phone[-4:] if selected_accounts[i-1].phone else "****"
                    result = detail['result']
                    
                    if result['success']:
                        results_text += f"✅ {phone_short}: موفق\n"
                    else:
                        results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                
                if len(results['details']) > 10:
                    results_text += f"\n... و {len(results['details']) - 10} مورد دیگر\n"
                
                results_text += f"\n✅ موفق: {results['success']}\n"
                results_text += f"❌ ناموفق: {results['failed']}"
                
                await progress_msg.edit(
                    results_text,
                    buttons=[
                        [Button.inline("💬 ارسال مجدد", b"send_message")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
                
                await self.db.log_action('bulk_message', user_id, f"{target} - {results['success']}/{total}")
                del self.user_states[user_id]
            
            elif step == 'react_link':
                # دریافت لینک پست
                post_link = event.message.text.strip()
                
                # تجزیه لینک پست
                try:
                    # فرمت: https://t.me/channel/123 یا https://t.me/c/1234567890/123
                    if '/c/' in post_link:
                        # لینک خصوصی
                        parts = post_link.split('/')
                        channel_id = int('-100' + parts[-2])
                        message_id = int(parts[-1])
                        channel_link = str(channel_id)
                    else:
                        # لینک عمومی
                        parts = post_link.split('/')
                        channel_link = parts[-2]
                        message_id = int(parts[-1])
                    
                    # دریافت اکانت‌های کاربر
                    accounts = await self.db.get_accounts(user_id)
                    active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                    
                    if not active_accounts:
                        await event.respond(
                            "❌ شما اکانت فعالی ندارید.",
                            buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                        )
                        del self.user_states[user_id]
                        return
                    
                    # ذخیره اطلاعات و انتقال به فرآیند رفرال/کشور پیشرفته
                    state['channel_link'] = channel_link
                    state['message_id'] = message_id
                    state['active_accounts'] = active_accounts
                    state['operation_type'] = 'react'
                    state['scenario_summary'] = f"❤️ **عملیات: ری‌اکشن و سین پست**\n📢 کانال: {channel_link}\n📨 پست: {message_id}"
                    
                    # نمایش دکمه‌های انتخاب کشور
                    countries = await self.db.get_countries(user_id)
                    if len(countries) > 1:
                        state['step'] = 'scenario_country'
                        country_text, buttons = self._create_country_buttons(countries)
                        await event.respond(country_text, buttons=buttons)
                    else:
                        state['selected_country'] = None
                        state['step'] = 'scenario_count'
                        await event.respond(
                            f"📊 **انتخاب تعداد اکانت**\n\n"
                            f"{state['scenario_summary']}\n\n"
                            f"شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                             f"چند تا اکانت برای ری‌اکشن استفاده شود؟\n\n"
                             f"💡 **گزینه‌ها:**\n"
                             f"• عدد بفرست (مثلاً `5`)\n"
                             f"• `/all` - همه اکانت‌ها\n"
                             f"• `/from 70` - از اکانت 70\n"
                             f"• `/from 70 to 100` - از 70 تا 100",
                             buttons=Button.inline("❌ لغو", b"cancel")
                         )
                    
                except (ValueError, IndexError) as e:
                    await event.respond(
                        "❌ لینک نامعتبر است!\n\n"
                        "لطفاً لینک را به فرمت صحیح ارسال کنید:\n"
                        "https://t.me/channel/123",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
            
            elif step == 'react_count':
                # دریافت تعداد اکانت
                count_input = event.message.text.strip()
                
                active_accounts = state['active_accounts']
                channel_link = state['channel_link']
                message_id = state['message_id']
                
                # تعیین تعداد اکانت
                if count_input.lower() == '/all':
                    selected_accounts = active_accounts
                else:
                    try:
                        count = int(count_input)
                        if count <= 0:
                            await event.respond(
                                "❌ تعداد باید بیشتر از صفر باشد!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return
                        selected_accounts = active_accounts[:min(count, len(active_accounts))]
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً یک عدد معتبر یا /all ارسال کنید.",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                total = len(selected_accounts)
                
                # ارسال پیام شروع
                progress_msg = await event.respond(
                    f"⏳ **شروع عملیات ری‌اکشن و سین**\n\n"
                    f"📢 کانال: {channel_link}\n"
                    f"📨 پست: {message_id}\n"
                    f"📊 تعداد اکانت‌ها: {total}\n"
                    f"❤️ هر اکانت: 1 ری‌اکشن تصادفی\n"
                    f"⏱ تاخیر بین هر عملیات: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                    f"لطفاً صبر کنید..."
                )
                
                # تابع callback برای بروزرسانی پیشرفت
                async def update_progress(current, total, message):
                    try:
                        await progress_msg.edit(
                            f"⏳ **در حال ری‌اکشن...**\n\n"
                            f"📢 کانال: {channel_link}\n"
                            f"📊 پیشرفت: {current}/{total}\n"
                            f"💬 {message}"
                        )
                    except:
                        pass
                
                # ری‌اکشن دسته‌جمعی
                session_paths = [acc.session_path for acc in selected_accounts]
                results = await self.reaction_manager.bulk_react_and_view(
                    session_paths,
                    channel_link,
                    message_id,
                    reaction_count=3,
                    progress_callback=update_progress
                )
                
                # نمایش نتایج
                results_text = "📊 **نتایج ری‌اکشن و سین:**\n\n"
                results_text += f"📢 کانال: {channel_link}\n"
                results_text += f"📨 پست: {message_id}\n\n"
                
                for i, detail in enumerate(results['details'][:10], 1):
                    phone_short = selected_accounts[i-1].phone[-4:] if selected_accounts[i-1].phone else "****"
                    result = detail['result']
                    
                    if result['success']:
                        reaction = result.get('reactions_sent', [''])[0] if result.get('reactions_sent') else '❓'
                        results_text += f"✅ {phone_short}: {reaction}\n"
                    else:
                        results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                
                if len(results['details']) > 10:
                    results_text += f"\n... و {len(results['details']) - 10} مورد دیگر\n"
                
                results_text += f"\n✅ موفق: {results['success']}\n"
                results_text += f"❌ ناموفق: {results['failed']}"
                
                await progress_msg.edit(
                    results_text,
                    buttons=[
                        [Button.inline("❤️ ری‌اکشن مجدد", b"react_post")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
                
                await self.db.log_action('bulk_reaction', user_id, f"{channel_link}/{message_id} - {results['success']}/{total}")
                del self.user_states[user_id]
            
            elif step == 'view_only_link':
                # دریافت لینک پست برای فقط سین
                post_link = event.message.text.strip()
                
                # تجزیه لینک پست
                try:
                    # فرمت: https://t.me/channel/123 یا https://t.me/c/1234567890/123
                    if '/c/' in post_link:
                        # لینک خصوصی
                        parts = post_link.split('/')
                        channel_id = int('-100' + parts[-2])
                        message_id = int(parts[-1])
                        channel_link = str(channel_id)
                    else:
                        # لینک عمومی
                        parts = post_link.split('/')
                        channel_link = parts[-2]
                        message_id = int(parts[-1])
                    
                    # دریافت اکانت‌های کاربر
                    accounts = await self.db.get_accounts(user_id)
                    active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                    
                    if not active_accounts:
                        await event.respond(
                            "❌ شما اکانت فعالی ندارید.",
                            buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                        )
                        del self.user_states[user_id]
                        return
                    
                    # ذخیره اطلاعات و انتقال به فرآیند رفرال/کشور پیشرفته
                    state['channel_link'] = channel_link
                    state['message_id'] = message_id
                    state['active_accounts'] = active_accounts
                    state['operation_type'] = 'view_only'
                    state['scenario_summary'] = f"👁 **عملیات: سین پیام بدون ری‌اکشن**\n📢 کانال: {channel_link}\n📨 پست: {message_id}"
                    
                    # نمایش دکمه‌های انتخاب کشور
                    countries = await self.db.get_countries(user_id)
                    if len(countries) > 1:
                        state['step'] = 'scenario_country'
                        country_text, buttons = self._create_country_buttons(countries)
                        await event.respond(country_text, buttons=buttons)
                    else:
                        state['selected_country'] = None
                        state['step'] = 'scenario_count'
                        await event.respond(
                            f"📊 **انتخاب تعداد اکانت**\n\n"
                            f"{state['scenario_summary']}\n\n"
                            f"شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                            f"چند تا اکانت برای سین استفاده شود؟\n\n"
                            f"💡 **گزینه‌ها:**\n"
                            f"• عدد بفرست (مثلاً `5`)\n"
                            f"• `/all` - همه اکانت‌ها\n"
                            f"• `/from 70` - از اکانت 70\n"
                            f"• `/from 70 to 100` - از 70 تا 100",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                    
                except (ValueError, IndexError) as e:
                    await event.respond(
                        "❌ لینک نامعتبر است!\n\n"
                        "لطفاً لینک را به فرمت صحیح ارسال کنید:\n"
                        "https://t.me/channel/123",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
            
            elif step == 'view_only_count':
                # دریافت تعداد اکانت برای فقط سین
                count_input = event.message.text.strip()
                
                active_accounts = state['active_accounts']
                channel_link = state['channel_link']
                message_id = state['message_id']
                
                # تعیین تعداد اکانت
                if count_input.lower() == '/all':
                    selected_accounts = active_accounts
                else:
                    try:
                        count = int(count_input)
                        if count <= 0:
                            await event.respond(
                                "❌ تعداد باید بیشتر از صفر باشد!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return
                        selected_accounts = active_accounts[:min(count, len(active_accounts))]
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً یک عدد معتبر یا /all ارسال کنید.",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                total = len(selected_accounts)
                
                # ارسال پیام شروع
                progress_msg = await event.respond(
                    f"⏳ **شروع عملیات سین**\n\n"
                    f"📢 کانال: {channel_link}\n"
                    f"📨 پست: {message_id}\n"
                    f"📊 تعداد اکانت‌ها: {total}\n"
                    f"👁 فقط سین (بدون ری‌اکشن)\n"
                    f"⏱ تاخیر بین هر عملیات: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                    f"لطفاً صبر کنید..."
                )
                
                # تابع callback برای بروزرسانی پیشرفت
                async def update_progress(current, total, message):
                    try:
                        await progress_msg.edit(
                            f"⏳ **در حال سین زدن...**\n\n"
                            f"📢 کانال: {channel_link}\n"
                            f"📊 پیشرفت: {current}/{total}\n"
                            f"💬 {message}"
                        )
                    except:
                        pass
                
                # سین دسته‌جمعی (بدون ری‌اکشن)
                session_paths = [acc.session_path for acc in selected_accounts]
                results = await self.reaction_manager.bulk_view_only(
                    session_paths,
                    channel_link,
                    message_id,
                    progress_callback=update_progress
                )
                
                # نمایش نتایج
                results_text = "📊 **نتایج سین:**\n\n"
                results_text += f"📢 کانال: {channel_link}\n"
                results_text += f"📨 پست: {message_id}\n\n"
                
                for i, detail in enumerate(results['details'][:10], 1):
                    phone_short = selected_accounts[i-1].phone[-4:] if selected_accounts[i-1].phone else "****"
                    result = detail['result']
                    
                    if result['success']:
                        results_text += f"✅ {phone_short}: سین زده شد\n"
                    else:
                        results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                
                if len(results['details']) > 10:
                    results_text += f"\n... و {len(results['details']) - 10} مورد دیگر\n"
                
                results_text += f"\n✅ موفق: {results['success']}\n"
                results_text += f"❌ ناموفق: {results['failed']}"
                
                await progress_msg.edit(
                    results_text,
                    buttons=[
                        [Button.inline("👁 سین مجدد", b"react_post")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
                
                await self.db.log_action('bulk_view_only', user_id, f"{channel_link}/{message_id} - {results['success']}/{total}")
                del self.user_states[user_id]
            
            elif step == 'block_target':
                # دریافت یوزرنیم یا آیدی کاربر برای بلاک
                target = event.message.text.strip()
                
                # دریافت اکانت‌های کاربر
                accounts = await self.db.get_accounts(user_id)
                active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                
                if not active_accounts:
                    await event.respond(
                        "❌ شما اکانت فعالی ندارید.",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                    del self.user_states[user_id]
                    return
                
                total = len(active_accounts)
                
                # ارسال پیام شروع
                progress_msg = await event.respond(
                    f"⏳ **شروع عملیات بلاک**\n\n"
                    f"👤 کاربر: {target}\n"
                    f"📊 تعداد اکانت‌ها: {total}\n"
                    f"⏱ تاخیر بین هر عملیات: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                    f"لطفاً صبر کنید..."
                )
                
                # تابع callback برای بروزرسانی پیشرفت
                async def update_progress(current, total, message):
                    try:
                        await progress_msg.edit(
                            f"⏳ **در حال بلاک...**\n\n"
                            f"👤 کاربر: {target}\n"
                            f"📊 پیشرفت: {current}/{total}\n"
                            f"💬 {message}"
                        )
                    except:
                        pass
                
                # بلاک دسته‌جمعی با تایمر
                session_paths = [acc.session_path for acc in active_accounts]
                results = await self.block_manager.bulk_block(
                    session_paths,
                    target,
                    progress_callback=update_progress
                )
                
                # نمایش نتایج
                results_text = "📊 **نتایج بلاک:**\n\n"
                results_text += f"👤 کاربر: {target}\n\n"
                
                for i, detail in enumerate(results['details'][:10], 1):  # نمایش 10 مورد اول
                    phone_short = active_accounts[i-1].phone[-4:] if active_accounts[i-1].phone else "****"
                    result = detail['result']
                    
                    if result['success']:
                        results_text += f"✅ {phone_short}: موفق\n"
                    else:
                        results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                
                if len(results['details']) > 10:
                    results_text += f"\n... و {len(results['details']) - 10} مورد دیگر\n"
                
                results_text += f"\n✅ موفق: {results['success']}\n"
                results_text += f"❌ ناموفق: {results['failed']}"
                
                await progress_msg.edit(
                    results_text,
                    buttons=[
                        [Button.inline("🚫 بلاک/انبلاک", b"block_user")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
                
                await self.db.log_action('bulk_block', user_id, f"{target} - {results['success']}/{total}")
                del self.user_states[user_id]
            
            elif step == 'unblock_target':
                # دریافت یوزرنیم یا آیدی کاربر برای انبلاک
                target = event.message.text.strip()
                
                # دریافت اکانت‌های کاربر
                accounts = await self.db.get_accounts(user_id)
                active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                
                if not active_accounts:
                    await event.respond(
                        "❌ شما اکانت فعالی ندارید.",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                    del self.user_states[user_id]
                    return
                
                total = len(active_accounts)
                
                # ارسال پیام شروع
                progress_msg = await event.respond(
                    f"⏳ **شروع عملیات انبلاک**\n\n"
                    f"👤 کاربر: {target}\n"
                    f"📊 تعداد اکانت‌ها: {total}\n"
                    f"⏱ تاخیر بین هر عملیات: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه\n\n"
                    f"لطفاً صبر کنید..."
                )
                
                # تابع callback برای بروزرسانی پیشرفت
                async def update_progress(current, total, message):
                    try:
                        await progress_msg.edit(
                            f"⏳ **در حال انبلاک...**\n\n"
                            f"👤 کاربر: {target}\n"
                            f"📊 پیشرفت: {current}/{total}\n"
                            f"💬 {message}"
                        )
                    except:
                        pass
                
                # انبلاک دسته‌جمعی با تایمر
                session_paths = [acc.session_path for acc in active_accounts]
                results = await self.block_manager.bulk_unblock(
                    session_paths,
                    target,
                    progress_callback=update_progress
                )
                
                # نمایش نتایج
                results_text = "📊 **نتایج انبلاک:**\n\n"
                results_text += f"👤 کاربر: {target}\n\n"
                
                for i, detail in enumerate(results['details'][:10], 1):  # نمایش 10 مورد اول
                    phone_short = active_accounts[i-1].phone[-4:] if active_accounts[i-1].phone else "****"
                    result = detail['result']
                    
                    if result['success']:
                        results_text += f"✅ {phone_short}: موفق\n"
                    else:
                        results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                
                if len(results['details']) > 10:
                    results_text += f"\n... و {len(results['details']) - 10} مورد دیگر\n"
                
                results_text += f"\n✅ موفق: {results['success']}\n"
                results_text += f"❌ ناموفق: {results['failed']}"
                
                await progress_msg.edit(
                    results_text,
                    buttons=[
                        [Button.inline("🚫 بلاک/انبلاک", b"block_user")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
                
                await self.db.log_action('bulk_unblock', user_id, f"{target} - {results['success']}/{total}")
                del self.user_states[user_id]
            
            elif step == 'set_backup_channel':
                # دریافت آیدی کانال بکاپ
                try:
                    channel_id = int(event.message.text.strip())
                    self.backup_manager.set_backup_channel(channel_id)
                    
                    # ذخیره در دیتابیس
                    await self.db.set_setting('backup_channel_id', str(channel_id))
                    
                    await event.respond(
                        f"✅ **کانال بکاپ تنظیم شد!**\n\n"
                        f"🆔 آیدی کانال: `{channel_id}`\n\n"
                        f"این تنظیمات در دیتابیس ذخیره شد و در اجراهای بعدی ربات، دیگر نیازی به تنظیم مجدد نیست.\n\n"
                        f"حالا می‌توانید بکاپ بگیرید.",
                        buttons=Button.inline("🔙 پنل ادمین", b"admin_panel")
                    )
                    
                    await self.db.log_action('set_backup_channel', user_id, str(channel_id))
                    del self.user_states[user_id]
                    
                except ValueError:
                    await event.respond(
                        "❌ آیدی نامعتبر است! لطفاً یک عدد صحیح ارسال کنید.\n"
                        "مثال: -1001234567890",
                        buttons=Button.inline("❌ لغو", b"admin_panel")
                    )
            
            elif step == 'waiting_note_single':
                # دریافت متن یادداشت برای تک ربات
                note_text = event.message.text.strip()
                bot_username = state['bot_username']
                scenario_text = state.get('scenario_text')
                
                # ذخیره یادداشت
                success = await self.note_manager.add_note(
                    user_id, bot_username, note_text, scenario_text
                )
                
                if success:
                    await event.respond(
                        f"✅ **یادداشت ذخیره شد!**\n\n"
                        f"🤖 ربات: @{bot_username}\n"
                        f"📝 یادداشت: {note_text[:100]}{'...' if len(note_text) > 100 else ''}\n\n"
                        f"💡 برای مشاهده یادداشت‌ها: `/notes @{bot_username}`",
                        buttons=[
                            [Button.inline("🎯 سناریو جدید", b"advanced_scenario")],
                            [Button.inline("📝 یادداشت‌های من", b"my_notes")],
                            [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                        ]
                    )
                    await self.db.log_action('add_note', user_id, f"@{bot_username}")
                else:
                    await event.respond(
                        "❌ خطا در ذخیره یادداشت!",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                
                del self.user_states[user_id]
            
            elif step == 'waiting_note_multi':
                # دریافت متن یادداشت برای چند ربات
                note_text = event.message.text.strip()
                bots_scenarios = state['bots_scenarios']
                current_index = state['current_bot_index']
                bot_username = bots_scenarios[current_index]['bot_username']
                scenario_text = state.get('scenario_text')
                
                # ذخیره یادداشت
                success = await self.note_manager.add_note(
                    user_id, bot_username, note_text, scenario_text
                )
                
                if success:
                    await event.respond(
                        f"✅ **یادداشت ذخیره شد!**\n\n"
                        f"🤖 ربات: @{bot_username}\n"
                        f"📝 یادداشت: {note_text[:100]}{'...' if len(note_text) > 100 else ''}"
                    )
                    await self.db.log_action('add_note', user_id, f"@{bot_username}")
                else:
                    await event.respond("❌ خطا در ذخیره یادداشت!")
                
                # رفتن به ربات بعدی
                next_index = current_index + 1
                
                if next_index < len(bots_scenarios):
                    # ربات بعدی وجود دارد
                    state['current_bot_index'] = next_index
                    state['step'] = 'ask_note_multi'
                    next_bot = bots_scenarios[next_index]['bot_username']
                    
                    await event.respond(
                        f"📝 **یادداشت برای ربات @{next_bot}**\n\n"
                        f"آیا می‌خواهید یادداشتی برای این ربات ثبت کنید؟",
                        buttons=[
                            [Button.inline("✅ بله، یادداشت می‌زنم", b"note_yes")],
                            [Button.inline("⏭ بعدی", b"note_skip")],
                            [Button.inline("❌ نه، برای هیچکدام", b"note_no_all")]
                        ]
                    )
                else:
                    # تمام رباتها تمام شدند
                    del self.user_states[user_id]
                    
                    await event.respond(
                        "✅ **تمام شد!**\n\n"
                        "سناریو با موفقیت اجرا شد و یادداشت‌ها ذخیره شدند.",
                        buttons=[
                            [Button.inline("🎯 سناریو جدید", b"advanced_scenario")],
                            [Button.inline("📝 یادداشت‌های من", b"my_notes")],
                            [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                        ]
                    )
            
            elif step == 'edit_note':
                # دریافت متن جدید یادداشت
                note_text = event.message.text.strip()
                note_id = state['note_id']
                
                # ویرایش یادداشت
                success = await self.note_manager.update_note(note_id, user_id, note_text)
                
                if success:
                    await event.respond(
                        f"✅ **یادداشت ویرایش شد!**\n\n"
                        f"📝 متن جدید: {note_text[:100]}{'...' if len(note_text) > 100 else ''}",
                        buttons=[
                            [Button.inline("📝 یادداشت‌های من", b"my_notes")],
                            [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                        ]
                    )
                    await self.db.log_action('edit_note', user_id, str(note_id))
                else:
                    await event.respond(
                        "❌ خطا در ویرایش یادداشت! شاید این یادداشت متعلق به شما نباشد.",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                
                del self.user_states[user_id]
            
            # ─── Pishi Automation Input Steps ────────────────────────────────
            elif step == 'pishi_input_mio_interval':
                try:
                    val = int(event.message.text.strip())
                    if val < 1: raise ValueError
                except ValueError:
                    await event.respond("❌ عدد معتبر وارد کنید (حداقل ۱)", buttons=Button.inline("❌ لغو", b"cancel"))
                    return
                account_id = state['pishi_account_id']
                await self.db.upsert_pishi_automation(user_id, account_id, mio_interval_minutes=val)
                del self.user_states[user_id]
                await event.respond(
                    f"✅ فاصله میو تنظیم شد: هر {val} دقیقه",
                    buttons=Button.inline("🔙 برگشت", f"pishi_acc_{account_id}".encode())
                )

            elif step == 'pishi_input_fish_interval':
                try:
                    val = int(event.message.text.strip())
                    if val < 1: raise ValueError
                except ValueError:
                    await event.respond("❌ عدد معتبر وارد کنید (حداقل ۱)", buttons=Button.inline("❌ لغو", b"cancel"))
                    return
                account_id = state['pishi_account_id']
                await self.db.upsert_pishi_automation(user_id, account_id, fish_interval_minutes=val)
                del self.user_states[user_id]
                await event.respond(
                    f"✅ فاصله ماهی تنظیم شد: هر {val} دقیقه",
                    buttons=Button.inline("🔙 برگشت", f"pishi_acc_{account_id}".encode())
                )

            elif step == 'pishi_input_transfer_hour':
                try:
                    val = int(event.message.text.strip())
                    if val < 0 or val > 23: raise ValueError
                except ValueError:
                    await event.respond("❌ عدد بین 0 تا 23 وارد کنید", buttons=Button.inline("❌ لغو", b"cancel"))
                    return
                account_id = state['pishi_account_id']
                await self.db.upsert_pishi_automation(user_id, account_id, transfer_hour=val)
                del self.user_states[user_id]
                await event.respond(
                    f"✅ ساعت انتقال تنظیم شد: {val:02d}:00",
                    buttons=Button.inline("🔙 برگشت", f"pishi_acc_{account_id}".encode())
                )

            elif step == 'pishi_input_target':
                from src.services.pishi_service import parse_message_link
                link = event.message.text.strip()
                _, msg_id = parse_message_link(link)
                if not msg_id:
                    await event.respond(
                        "❌ لینک نامعتبر! مثال: `https://t.me/meavmeacv/113309`",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    return
                account_id = state['pishi_account_id']
                await self.db.upsert_pishi_automation(user_id, account_id, transfer_target_msg_id=msg_id)
                del self.user_states[user_id]
                await event.respond(
                    f"✅ target انتقال تنظیم شد: msg #{msg_id}",
                    buttons=Button.inline("🔙 برگشت", f"pishi_acc_{account_id}".encode())
                )

            elif step == 'pishi_input_global_group':
                group_input = event.message.text.strip()
                if not group_input.startswith('@'):
                    group_input = '@' + group_input.lstrip('@')
                # آپدیت همه اکانت‌های این کاربر
                cfgs = await self.db.get_user_pishi_automations(user_id)
                accounts = await self.db.get_accounts(user_id)
                active = [a for a in accounts if a.status == 'active']
                for acc in active:
                    await self.db.upsert_pishi_automation(user_id, acc.id, group_username=group_input)
                del self.user_states[user_id]
                await event.respond(
                    f"✅ گروه پیشی برای همه اکانت‌ها تنظیم شد: `{group_input}`",
                    buttons=Button.inline("🔙 اتوماسیون", b"pishi_automation")
                )

            elif step == 'pishi_p_mio_interval':
                try:
                    val = int(event.message.text.strip())
                    if val < 1: raise ValueError
                except ValueError:
                    await event.respond("❌ عدد معتبر وارد کنید (حداقل ۱)", buttons=Button.inline("❌ لغو", b"pishi_menu"))
                    return
                await self.db.upsert_pishi_panel(user_id, mio_interval_minutes=val)
                del self.user_states[user_id]
                await event.respond(f"✅ زمان میو: هر {val} دقیقه", buttons=Button.inline("🔙 پنل پیشی", b"pishi_menu"))

            elif step == 'pishi_p_fish_interval':
                try:
                    val = int(event.message.text.strip())
                    if val < 1: raise ValueError
                except ValueError:
                    await event.respond("❌ عدد معتبر وارد کنید (حداقل ۱)", buttons=Button.inline("❌ لغو", b"pishi_menu"))
                    return
                await self.db.upsert_pishi_panel(user_id, fish_interval_minutes=val)
                del self.user_states[user_id]
                await event.respond(f"✅ زمان ماهی: هر {val} دقیقه", buttons=Button.inline("🔙 پنل پیشی", b"pishi_menu"))

            elif step == 'pishi_p_trans_settings':
                text_in = event.message.text.strip()
                from src.services.pishi_service import parse_message_link
                if text_in.lower().startswith('hour:'):
                    try:
                        h = int(text_in.split(':')[1])
                        if h < 0 or h > 23: raise ValueError
                        await self.db.upsert_pishi_panel(user_id, transfer_hour=h)
                        del self.user_states[user_id]
                        await event.respond(f"✅ ساعت انتقال: {h:02d}:00", buttons=Button.inline("🔙 پنل پیشی", b"pishi_menu"))
                    except (ValueError, IndexError):
                        await event.respond("❌ فرمت اشتباه. مثال: `hour:0`", buttons=Button.inline("❌ لغو", b"pishi_menu"))
                else:
                    _, msg_id = parse_message_link(text_in)
                    if not msg_id:
                        await event.respond("❌ لینک نامعتبر! مثال: `https://t.me/meavmeacv/113309`", buttons=Button.inline("❌ لغو", b"pishi_menu"))
                        return
                    await self.db.upsert_pishi_panel(user_id, transfer_target_msg_id=msg_id)
                    del self.user_states[user_id]
                    await event.respond(f"✅ target انتقال: msg #{msg_id}", buttons=Button.inline("🔙 پنل پیشی", b"pishi_menu"))

            elif step == 'pishi_p_account_count':
                text_in = event.message.text.strip()
                accounts = await self.db.get_accounts(user_id)
                active = [a for a in accounts if a.status == 'active' and a.session_path]
                if text_in.lower() == '/all':
                    val = len(active)
                    label = 'همه'
                else:
                    try:
                        val = int(text_in)
                        if val < 1 or val > len(active): raise ValueError
                        label = str(val)
                    except ValueError:
                        await event.respond(f"❌ عدد بین 1 تا {len(active)} یا /all", buttons=Button.inline("❌ لغو", b"pishi_menu"))
                        return
                await self.db.upsert_pishi_panel(user_id, account_count=val)
                del self.user_states[user_id]
                await event.respond(f"✅ تعداد اکانت: {label}", buttons=Button.inline("🔙 پنل پیشی", b"pishi_menu"))

            # ─── Pishi Transfer Steps ─────────────────────────────────────────
            elif step == 'pishi_transfer_group':
                group_input = event.message.text.strip()
                # normalize
                if group_input.startswith('https://t.me/'):
                    group_input = '@' + group_input.split('t.me/')[-1].split('/')[0]
                elif not group_input.startswith('@'):
                    group_input = '@' + group_input.lstrip('@')

                state['pishi_group'] = group_input
                state['step'] = 'pishi_transfer_link'
                await event.respond(
                    f"💰 **انتقال موجودی — مرحله ۲/۳**\n\n"
                    f"📢 گروه: `{group_input}`\n\n"
                    f"لینک پیام کسی که می‌خواید موجودی بهش منتقل بشه:\n\n"
                    f"مثال: `https://t.me/meavmeacv/113309`",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )

            elif step == 'pishi_transfer_link':
                from src.services.pishi_service import parse_message_link
                link = event.message.text.strip()
                group_from_link, msg_id = parse_message_link(link)

                if not msg_id:
                    await event.respond(
                        "❌ لینک نامعتبر است!\n\n"
                        "فرمت صحیح: `https://t.me/channel/12345`",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    return

                state['pishi_target_msg_id'] = msg_id
                state['pishi_target_link'] = link
                state['step'] = 'pishi_transfer_count'

                active_accounts = state.get('active_accounts', [])
                await event.respond(
                    f"💰 **انتقال موجودی — مرحله ۳/۳**\n\n"
                    f"📢 گروه: `{state['pishi_group']}`\n"
                    f"🔗 پیام target: `{link}`\n\n"
                    f"📊 اکانت‌های فعال: {len(active_accounts)}\n\n"
                    f"چند اکانت انتقال بدن؟\n"
                    f"• عدد (مثلاً `5`) یا `/all`",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )

            elif step == 'pishi_transfer_count':
                count_input = event.message.text.strip()
                active_accounts = state.get('active_accounts', [])

                try:
                    selected = self._select_accounts(count_input, active_accounts)
                except ValueError as e:
                    await event.respond(f"❌ {e}", buttons=Button.inline("❌ لغو", b"cancel"))
                    return

                group = state['pishi_group']
                target_msg_id = state['pishi_target_msg_id']
                target_link = state['pishi_target_link']
                total = len(selected)

                del self.user_states[user_id]

                progress_msg = await event.respond(
                    f"⏳ **شروع انتقال موجودی...**\n\n"
                    f"📢 گروه: `{group}`\n"
                    f"👥 تعداد اکانت: {total}\n\n"
                    f"لطفاً صبر کنید...",
                    buttons=Button.inline("❌ لغو", b"cancel_scenario")
                )

                cancel_flag = {'cancelled': False}
                self.running_operations[user_id] = cancel_flag

                async def run_pishi_transfer():
                    try:
                        session_paths = [acc.session_path for acc in selected]

                        async def update_progress(current, total, message):
                            try:
                                await progress_msg.edit(
                                    f"⏳ **در حال انتقال...**\n\n"
                                    f"📊 پیشرفت: {current}/{total}\n"
                                    f"💬 {message}",
                                    buttons=Button.inline("❌ لغو", b"cancel_scenario")
                                )
                            except Exception:
                                pass

                        results = await self.pishi_service.bulk_transfer(
                            session_paths=session_paths,
                            group_username=group,
                            target_message_id=target_msg_id,
                            progress_callback=update_progress,
                            workers=1,
                            cancel_flag=cancel_flag,
                        )

                        if cancel_flag.get('cancelled'):
                            await progress_msg.edit(
                                "❌ **انتقال لغو شد!**",
                                buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                            )
                            return

                        total_mio = results['total_transferred']

                        result_text = (
                            f"✅ **انتقال موجودی تکمیل شد!**\n\n"
                            f"📢 گروه: `{group}`\n"
                            f"💰 مجموع انتقال: {total_mio:,} میو پوینت\n"
                            f"✅ موفق: {results['success']}\n"
                            f"❌ ناموفق: {results['failed']}\n\n"
                        )

                        # نمایش جزئیات (۱۰ تا اول)
                        for i, detail in enumerate(results['details'][:10], 1):
                            acc = selected[i - 1]
                            phone_short = (acc.phone or '')[-4:] or '****'
                            r = detail['result']
                            if r.get('final_success'):
                                result_text += f"✅ {phone_short}: {r['balance']:,} 🪙 تایید شد\n"
                            elif r['success'] and r['balance'] > 0:
                                result_text += f"⚠️ {phone_short}: {r['balance']:,} 🪙 (تایید نشد)\n"
                            elif r['success']:
                                result_text += f"⬛ {phone_short}: موجودی صفر\n"
                            else:
                                result_text += f"❌ {phone_short}: {r['message'][:30]}\n"

                        if len(results['details']) > 10:
                            result_text += f"\n... و {len(results['details']) - 10} اکانت دیگر"

                        await progress_msg.edit(
                            result_text,
                            buttons=[
                                [Button.inline("💰 انتقال مجدد", b"pishi_transfer")],
                                [Button.inline("🔙 پیشی", b"pishi_menu")]
                            ]
                        )

                    except Exception as e:
                        logger.exception(f"خطا در انتقال پیشی: {e}")
                        await progress_msg.edit(
                            f"❌ **خطا در انتقال:**\n\n{str(e)[:200]}",
                            buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                        )
                    finally:
                        if user_id in self.running_operations:
                            del self.running_operations[user_id]

                asyncio.create_task(run_pishi_transfer())

            # ─── Scheduled Scenario Steps ────────────────────────────────────
            elif step == 'sched_name':
                name = event.message.text.strip()
                if not name:
                    await event.respond("❌ نام نمی‌تواند خالی باشد.", buttons=Button.inline("❌ لغو", b"cancel"))
                    return
                state['sched_name'] = name
                state['step'] = 'sched_scenario'
                await event.respond(
                    f"⏰ **سناریو زمانبندی — مرحله ۲/۴**\n\n"
                    f"📌 نام: {name}\n\n"
                    f"متن سناریو را ارسال کن (همان فرمت سناریو پیشرفته):\n\n"
                    f"مثال:\n"
                    f"`@mybot\nstart: ref123\nclick: #0`",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )

            elif step == 'sched_scenario':
                scenario_text = event.message.text.strip()
                lines = scenario_text.split('\n')
                bot_count = sum(1 for line in lines if line.strip().startswith('@'))
                if bot_count == 0:
                    await event.respond(
                        "❌ سناریو باید با یوزرنیم ربات شروع شود! (مثال: @bot_name)",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    return
                state['sched_scenario'] = scenario_text
                state['step'] = 'sched_count'
                
                accounts = await self.db.get_accounts(user_id)
                active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                state['active_accounts'] = active_accounts
                
                await event.respond(
                    f"⏰ **سناریو زمانبندی — مرحله ۳/۴**\n\n"
                    f"📊 اکانت‌های فعال: {len(active_accounts)}\n\n"
                    f"چند اکانت هر بار استفاده شود؟\n"
                    f"• عدد (مثلاً `10`)\n"
                    f"• `/all` — همه اکانت‌ها",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )

            elif step == 'sched_count':
                count_input = event.message.text.strip()
                active_accounts = state.get('active_accounts', [])
                try:
                    selected = self._select_accounts(count_input, active_accounts)
                except ValueError as e:
                    await event.respond(f"❌ {e}", buttons=Button.inline("❌ لغو", b"cancel"))
                    return
                state['sched_count'] = count_input
                state['step'] = 'sched_interval'
                await event.respond(
                    f"⏰ **سناریو زمانبندی — مرحله ۴/۴**\n\n"
                    f"📊 تعداد اکانت هر بار: {len(selected)}\n\n"
                    f"هر چند دقیقه اجرا شود؟\n"
                    f"• عدد دقیقه (مثلاً `50` یا `120`)",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )

            elif step == 'sched_interval':
                try:
                    interval = int(event.message.text.strip())
                    if interval < 1:
                        raise ValueError("حداقل 1 دقیقه")
                except ValueError:
                    await event.respond("❌ یک عدد معتبر (دقیقه) ارسال کنید.", buttons=Button.inline("❌ لغو", b"cancel"))
                    return
                
                name = state['sched_name']
                scenario_text = state['sched_scenario']
                account_count = state['sched_count']
                
                # ذخیره در DB
                schedule_id = await self.db.add_scheduled_scenario(
                    user_id=user_id,
                    name=name,
                    scenario_text=scenario_text,
                    account_count=account_count,
                    workers=3,
                    interval_minutes=interval
                )
                
                if schedule_id:
                    # شروع task
                    task = asyncio.create_task(self._run_scheduled_scenario(schedule_id))
                    self.scheduled_tasks[schedule_id] = task
                    
                    await event.respond(
                        f"✅ **سناریو زمانبندی ثبت شد!**\n\n"
                        f"📌 نام: {name}\n"
                        f"⏰ تکرار: هر {interval} دقیقه\n"
                        f"👥 اکانت‌ها: {account_count}\n"
                        f"🆔 شناسه: #{schedule_id}\n\n"
                        f"سناریو اولین بار {interval} دقیقه دیگر اجرا می‌شود.",
                        buttons=[
                            [Button.inline("⏰ سناریو زمانبندی", b"scheduled_scenario")],
                            [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                        ]
                    )
                else:
                    await event.respond(
                        "❌ خطا در ذخیره سناریو.",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                
                del self.user_states[user_id]

            elif step == 'scenario_input':
                # دریافت سناریو
                scenario_text = event.message.text.strip()
                
                # بررسی اینکه آیا چند ربات داریم یا یک ربات
                lines = scenario_text.split('\n')
                bot_count = sum(1 for line in lines if line.strip().startswith('@'))
                
                if bot_count == 0:
                    await event.respond(
                        "❌ سناریو باید با یوزرنیم ربات شروع شود! (مثال: @bot_name)",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    return
                
                # اگر چند ربات داریم
                if bot_count > 1:
                    # تجزیه سناریو چند ربات
                    bots_scenarios = self.bot_automation.parse_multi_bot_scenario(scenario_text)
                    
                    if not bots_scenarios:
                        await event.respond(
                            "❌ سناریو نامعتبر است! لطفاً فرمت صحیح را رعایت کنید.",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                    
                    # دریافت اکانت‌های کاربر
                    accounts = await self.db.get_accounts(user_id)
                    active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                    
                    if not active_accounts:
                        await event.respond(
                            "❌ شما اکانت فعالی ندارید.",
                            buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                        )
                        del self.user_states[user_id]
                        return
                    
                    # نمایش خلاصه سناریو
                    scenario_summary = f"🤖 **{len(bots_scenarios)} ربات:**\n\n"
                    for bot_data in bots_scenarios:
                        bot_username = bot_data['bot_username']
                        scenario = bot_data['scenario']
                        scenario_summary += f"@{bot_username} ({len(scenario)} مرحله)\n"
                    
                    # ذخیره اطلاعات
                    state['multi_bot'] = True
                    state['bots_scenarios'] = bots_scenarios
                    state['scenario_summary'] = scenario_summary
                    state['active_accounts'] = active_accounts
                    state['scenario_text'] = scenario_text  # ذخیره متن کامل سناریو
                    state['step'] = 'scenario_country'
                    
                    # دریافت لیست کشورها
                    countries = await self.db.get_countries(user_id)
                    
                    if len(countries) > 1:
                        # کاربر اکانت از چند کشور داره
                        country_text, buttons = self._create_country_buttons(countries)
                        
                        await event.respond(country_text, buttons=buttons)
                    else:
                        # فقط یک کشور داره یا هیچ کشوری نداره، مستقیم بریم سراغ تعداد اکانت
                        state['selected_country'] = None  # همه
                        state['step'] = 'scenario_count'
                        
                        # بررسی پیشرفت قبلی
                        progress = await self.db.get_scenario_progress(user_id, scenario_text)
                        
                        if progress and progress['last_account_index'] > 0:
                            # سناریو قبلاً شروع شده
                            last_index = progress['last_account_index']
                            total = progress['total_accounts']
                            
                            buttons = [
                                [Button.inline(f"▶️ ادامه از اکانت {last_index + 1}", b"resume_scenario")],
                                [Button.inline("🔄 شروع از اول", b"restart_scenario")],
                                [Button.inline("🎯 انتخاب دستی", b"manual_select_scenario")],
                                [Button.inline("❌ لغو", b"cancel")]
                            ]
                            
                            await event.respond(
                                f"⚠️ **سناریو قبلاً شروع شده!**\n\n"
                                f"{scenario_summary}\n"
                                f"📊 پیشرفت قبلی: {last_index}/{total} اکانت\n\n"
                                f"می‌خواهید از کجا ادامه دهید؟",
                                buttons=buttons
                            )
                        else:
                            # سناریو جدید
                            await event.respond(
                                f"📊 **انتخاب تعداد اکانت**\n\n"
                                f"{scenario_summary}\n"
                                f"شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                                f"چند تا اکانت برای اجرای سناریو استفاده شود؟\n\n"
                                f"💡 **گزینه‌ها:**\n"
                                f"• عدد بفرست (مثلاً `5`) - از اول شروع میشه\n"
                                f"• `/all` - همه اکانت‌ها\n"
                                f"• `/from 70` - از اکانت 70 شروع کن\n"
                                f"• `/from 70 to 100` - از 70 تا 100",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                
                else:
                    # یک ربات (روش قبلی)
                    bot_username = lines[0].strip().lstrip('@')
                    scenario_commands = '\n'.join(lines[1:])
                    
                    # تجزیه سناریو
                    scenario = self.bot_automation.parse_scenario(scenario_commands)
                    
                    if not scenario:
                        await event.respond(
                            "❌ سناریو نامعتبر است! لطفاً فرمت صحیح را رعایت کنید.",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                    
                    # دریافت اکانت‌های کاربر
                    accounts = await self.db.get_accounts(user_id)
                    active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                    
                    if not active_accounts:
                        await event.respond(
                            "❌ شما اکانت فعالی ندارید.",
                            buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                        )
                        del self.user_states[user_id]
                        return
                    
                    # نمایش خلاصه سناریو
                    scenario_summary = f"🤖 ربات: @{bot_username}\n📋 مراحل:\n"
                    for i, step in enumerate(scenario, 1):
                        action = step['action']
                        value = step['value'][:30] if len(step['value']) > 30 else step['value']
                        scenario_summary += f"{i}. {action}: {value}\n"
                    
                    # ذخیره اطلاعات
                    state['multi_bot'] = False
                    state['bot_username'] = bot_username
                    state['scenario'] = scenario
                    state['scenario_summary'] = scenario_summary
                    state['active_accounts'] = active_accounts
                    state['scenario_text'] = scenario_text  # ذخیره متن کامل سناریو
                    state['step'] = 'scenario_country'
                    
                    # دریافت لیست کشورها
                    countries = await self.db.get_countries(user_id)
                    
                    if len(countries) > 1:
                        # کاربر اکانت از چند کشور داره
                        country_text, buttons = self._create_country_buttons(countries)
                        
                        await event.respond(country_text, buttons=buttons)
                    else:
                        # فقط یک کشور داره یا هیچ کشوری نداره، مستقیم بریم سراغ تعداد اکانت
                        state['selected_country'] = None  # همه
                        state['step'] = 'scenario_count'
                        
                        # بررسی پیشرفت قبلی
                        progress = await self.db.get_scenario_progress(user_id, scenario_text)
                        
                        if progress and progress['last_account_index'] > 0:
                            # سناریو قبلاً شروع شده
                            last_index = progress['last_account_index']
                            total = progress['total_accounts']
                            
                            buttons = [
                                [Button.inline(f"▶️ ادامه از اکانت {last_index + 1}", b"resume_scenario")],
                                [Button.inline("🔄 شروع از اول", b"restart_scenario")],
                                [Button.inline("🎯 انتخاب دستی", b"manual_select_scenario")],
                                [Button.inline("❌ لغو", b"cancel")]
                            ]
                            
                            await event.respond(
                                f"⚠️ **سناریو قبلاً شروع شده!**\n\n"
                                f"{scenario_summary}\n"
                                f"📊 پیشرفت قبلی: {last_index}/{total} اکانت\n\n"
                                f"می‌خواهید از کجا ادامه دهید؟",
                                buttons=buttons
                            )
                        else:
                            # سناریو جدید
                            await event.respond(
                                f"📊 **انتخاب تعداد اکانت**\n\n"
                                f"{scenario_summary}\n"
                                f"شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                                f"چند تا اکانت برای اجرای سناریو استفاده شود؟\n\n"
                                f"💡 **گزینه‌ها:**\n"
                                f"• عدد بفرست (مثلاً `5`) - از اول شروع میشه\n"
                                f"• `/all` - همه اکانت‌ها\n"
                                f"• `/from 70` - از اکانت 70 شروع کن\n"
                                f"• `/from 70 to 100` - از 70 تا 100",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
            
            elif step == 'scenario_country':
                # دریافت انتخاب کشور
                country_input = event.message.text.strip().upper()
                
                if country_input == '/ALL':
                    # همه کشورها
                    state['selected_country'] = None
                    filtered_accounts = state['active_accounts']
                else:
                    # کشور خاص
                    state['selected_country'] = country_input
                    # فیلتر اکانت‌ها بر اساس کشور
                    filtered_accounts = [acc for acc in state['active_accounts'] if acc.country_code == country_input]
                    
                    if not filtered_accounts:
                        from src.utils.countries import get_country_name
                        await event.respond(
                            f"❌ هیچ اکانتی از {get_country_name(country_input)} پیدا نشد!\n\n"
                            f"💡 `/all` بفرست برای استفاده از همه کشورها",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                # بروزرسانی لیست اکانت‌های فعال
                state['active_accounts'] = filtered_accounts
                state['step'] = 'scenario_count'
                
                # بررسی پیشرفت قبلی
                scenario_text = state['scenario_text']
                scenario_summary = state['scenario_summary']
                progress = await self.db.get_scenario_progress(user_id, scenario_text)
                
                if progress and progress['last_account_index'] > 0:
                    # سناریو قبلاً شروع شده
                    last_index = progress['last_account_index']
                    total = progress['total_accounts']
                    
                    buttons = [
                        [Button.inline(f"▶️ ادامه از اکانت {last_index + 1}", b"resume_scenario")],
                        [Button.inline("🔄 شروع از اول", b"restart_scenario")],
                        [Button.inline("🎯 انتخاب دستی", b"manual_select_scenario")],
                        [Button.inline("❌ لغو", b"cancel")]
                    ]
                    
                    await event.respond(
                        f"⚠️ **سناریو قبلاً شروع شده!**\n\n"
                        f"{scenario_summary}\n"
                        f"📊 پیشرفت قبلی: {last_index}/{total} اکانت\n\n"
                        f"می‌خواهید از کجا ادامه دهید؟",
                        buttons=buttons
                    )
                else:
                    # سناریو جدید
                    country_info = ""
                    if state.get('selected_country'):
                        from src.utils.countries import get_country_name
                        country_info = f"🌍 کشور: {get_country_name(state['selected_country'])}\n"
                    
                    await event.respond(
                        f"📊 **انتخاب تعداد اکانت**\n\n"
                        f"{scenario_summary}\n"
                        f"{country_info}"
                        f"شما {len(filtered_accounts)} اکانت فعال دارید.\n\n"
                        f"چند تا اکانت برای اجرای سناریو استفاده شود؟\n\n"
                        f"💡 **گزینه‌ها:**\n"
                        f"• عدد بفرست (مثلاً `5`) - از اول شروع میشه\n"
                        f"• `/all` - همه اکانت‌ها\n"
                        f"• `/from 70` - از اکانت 70 شروع کن\n"
                        f"• `/from 70 to 100` - از 70 تا 100",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
            
            elif step == 'scenario_count':
                # دریافت تعداد اکانت
                count_input = event.message.text.strip()
                
                active_accounts = state['active_accounts']
                scenario_summary = state['scenario_summary']
                is_multi_bot = state.get('multi_bot', False)
                start_index = state.get('start_index', 0)  # شروع از کجا
                resume_mode = state.get('resume_mode', False)
                
                # تعیین تعداد اکانت
                if count_input.lower() == '/all':
                    selected_accounts = active_accounts[start_index:]  # از start_index شروع کن
                
                elif count_input.lower().startswith('/from'):
                    # پردازش دستورات /from
                    parts = count_input.lower().split()
                    
                    try:
                        if len(parts) == 2:
                            # /from 70 - از 70 تا آخر
                            start_num = int(parts[1])
                            if start_num < 1 or start_num > len(active_accounts):
                                await event.respond(
                                    f"❌ شماره اکانت باید بین 1 تا {len(active_accounts)} باشد!",
                                    buttons=Button.inline("❌ لغو", b"cancel")
                                )
                                return
                            start_index = start_num - 1  # تبدیل به index (از 0 شروع میشه)
                            selected_accounts = active_accounts[start_index:]
                            resume_mode = True
                            state['start_index'] = start_index
                            state['resume_mode'] = True
                        
                        elif len(parts) == 4 and parts[2] == 'to':
                            # /from 70 to 100 - از 70 تا 100
                            start_num = int(parts[1])
                            end_num = int(parts[3])
                            
                            if start_num < 1 or start_num > len(active_accounts):
                                await event.respond(
                                    f"❌ شماره شروع باید بین 1 تا {len(active_accounts)} باشد!",
                                    buttons=Button.inline("❌ لغو", b"cancel")
                                )
                                return
                            
                            if end_num < start_num or end_num > len(active_accounts):
                                await event.respond(
                                    f"❌ شماره پایان باید بین {start_num} تا {len(active_accounts)} باشد!",
                                    buttons=Button.inline("❌ لغو", b"cancel")
                                )
                                return
                            
                            start_index = start_num - 1
                            end_index = end_num
                            selected_accounts = active_accounts[start_index:end_index]
                            resume_mode = True
                            state['start_index'] = start_index
                            state['resume_mode'] = True
                        
                        else:
                            await event.respond(
                                "❌ فرمت نامعتبر!\n\n"
                                "فرمت‌های صحیح:\n"
                                "• `/from 70` - از 70 تا آخر\n"
                                "• `/from 70 to 100` - از 70 تا 100",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return
                    
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً اعداد معتبر وارد کنید!",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                else:
                    try:
                        count = int(count_input)
                        if count <= 0:
                            await event.respond(
                                "❌ تعداد باید بیشتر از صفر باشد!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return
                        selected_accounts = active_accounts[start_index:start_index + count]  # از start_index شروع کن
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً یک عدد معتبر یا دستور صحیح ارسال کنید.\n\n"
                            "مثال‌ها:\n"
                            "• `5` - 5 اکانت از اول\n"
                            "• `/all` - همه اکانت‌ها\n"
                            "• `/from 70` - از اکانت 70\n"
                            "• `/from 70 to 100` - از 70 تا 100",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                # ذخیره اکانت‌های انتخاب شده
                state['selected_accounts'] = selected_accounts
                state['step'] = 'scenario_workers'
                
                # پرسیدن تعداد worker
                await event.respond(
                    f"⚡ **سرعت اجرا**\n\n"
                    f"📊 تعداد اکانت‌های انتخاب شده: {len(selected_accounts)}\n\n"
                    f"چند تا اکانت همزمان اجرا شوند؟\n\n"
                    f"💡 **توصیه:**\n"
                    f"• `1` - یکی یکی (کندتر ولی امن‌تر)\n"
                    f"• `3` - 3 تا همزمان (متعادل) ✅\n"
                    f"• `5` - 5 تا همزمان (سریع‌تر)\n"
                    f"• `10` - 10 تا همزمان (خیلی سریع ولی ممکنه مشکل بده)\n\n"
                    f"⚠️ **نکته:** هرچه عدد بیشتر، سریع‌تر ولی فشار بیشتر روی سرور",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )
            
            elif step == 'scenario_workers':
                # دریافت تعداد worker
                try:
                    workers = int(event.message.text.strip())
                    if workers < 1:
                        await event.respond(
                            "❌ تعداد باید حداقل 1 باشد!",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                    if workers > 20:
                        await event.respond(
                            "❌ حداکثر 20 worker مجاز است!",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                except ValueError:
                    await event.respond(
                        "❌ لطفاً یک عدد معتبر ارسال کنید (مثلاً 3)",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    return
                
                # ذخیره تعداد worker
                state['workers'] = workers
                selected_accounts = state['selected_accounts']
                
                # پرسیدن بازه زمانی
                await self._ask_time_limit(event, user_id, len(selected_accounts), workers, 'scenario_time_limit')
            
            elif step == 'scenario_time_limit':
                # دریافت بازه زمانی
                text = event.message.text.strip()
                
                custom_delay = None  # تاخیر سفارشی (None = استفاده از تاخیر پیش‌فرض)
                time_limit_text = ""
                
                if text.lower() != '/skip':
                    try:
                        custom_delay, time_limit_text = self._parse_time_limit(
                            text, 
                            len(state['selected_accounts']), 
                            state['workers']
                        )
                    except ValueError as e:
                        await event.respond(
                            f"❌ {str(e)}",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                # دریافت اطلاعات از state
                selected_accounts = state['selected_accounts']
                workers = state['workers']
                scenario_summary = state['scenario_summary']
                is_multi_bot = state.get('multi_bot', False)
                start_index = state.get('start_index', 0)
                resume_mode = state.get('resume_mode', False)
                
                # بررسی قفل سشن‌ها - فقط برای اطلاع‌رسانی
                # قفل واقعی per-session در حین اجرا انجام می‌شه
                session_paths = [acc.session_path for acc in selected_accounts]
                total = len(selected_accounts)
                scenario_text = state.get('scenario_text', '')
                
                is_tree_referral = '{parent_ref}' in scenario_text or '{parent_ref_id}' in scenario_text
                if is_tree_referral:
                    workers = 1
                
                # ایجاد flag برای لغو و توقف عملیات
                cancel_flag = {'cancelled': False, 'paused': False}
                self.running_operations[user_id] = cancel_flag
                
                # ارسال پیام شروع با دکمه‌های کنترل
                resume_text = f"▶️ ادامه از اکانت {start_index + 1}\n" if resume_mode else ""
                if is_tree_referral:
                    worker_text = "🌲 رفرال درختی: اجرای تک‌به‌تک (مجموعه زنجیره‌ای)\n"
                else:
                    worker_text = f"⚡ همزمان: {workers} اکانت\n" if workers > 1 else ""
                
                # تعیین تاخیر نهایی
                if custom_delay is not None:
                    delay_text = f"⏱ تاخیر: ~{custom_delay} ثانیه (محاسبه شده برای بازه زمانی)\n"
                else:
                    delay_text = f"⏱ تاخیر: {Config.DELAY_BETWEEN_ACTIONS}-{Config.DELAY_BETWEEN_ACTIONS + Config.DELAY_RANDOM_RANGE} ثانیه (پیش‌فرض)\n"
                
                progress_msg = await event.respond(
                    f"⏳ **شروع اجرای سناریو**\n\n"
                    f"{scenario_summary}\n"
                    f"{resume_text}"
                    f"📊 تعداد اکانت‌ها: {total}\n"
                    f"{worker_text}"
                    f"{time_limit_text}"
                    f"{delay_text}\n"
                    f"✅ **عملیات در پس‌زمینه شروع شد!**\n"
                    f"💡 می‌توانید کارهای دیگر انجام دهید.",
                    buttons=[
                        [Button.inline("⏸ توقف موقت", b"pause_scenario")],
                        [Button.inline("🛑 لغو کامل", b"cancel_scenario")]
                    ]
                )
                
                # دریافت نوع عملیات
                op_type = state.get('operation_type', 'scenario')
                
                # تابع اجرای عملیات‌های دسته‌جمعی ساده در پس‌زمینه
                async def run_bulk_operation_background():
                    try:
                        session_paths = [acc.session_path for acc in selected_accounts]
                        total = len(selected_accounts)
                        
                        # تابع بروزرسانی پیشرفت
                        async def update_progress(current, total, message):
                            try:
                                await progress_msg.edit(
                                    f"⏳ **در حال اجرا...**\n\n"
                                    f"📊 پیشرفت: {current}/{total}\n"
                                    f"💬 {message}"
                                )
                            except:
                                pass
                        
                        results = None
                        results_text = ""
                        op_log_name = ""
                        op_log_detail = ""
                        btn_retry = ""
                        
                        if op_type == 'join_channel':
                            results = await self.channel_manager.bulk_join(
                                session_paths, state['channel_link'],
                                progress_callback=update_progress, workers=workers, custom_delay=custom_delay
                            )
                            results_text = "📊 **نتایج جوین:**\n\n"
                            btn_retry = "join_channel"
                            op_log_name = "bulk_join"
                            op_log_detail = f"{state['channel_link']} - {results['success']}/{total}"
                            
                        elif op_type == 'leave_channel':
                            results = await self.channel_manager.bulk_leave(
                                session_paths, state['channel_link'],
                                progress_callback=update_progress, workers=workers, custom_delay=custom_delay
                            )
                            results_text = "📊 **نتایج لفت:**\n\n"
                            btn_retry = "leave_channel"
                            op_log_name = "bulk_leave"
                            op_log_detail = f"{state['channel_link']} - {results['success']}/{total}"
                            
                        elif op_type == 'start_referral':
                            results = await self.referral_manager.bulk_start_bot(
                                session_paths, state['bot_username'], state['start_param'],
                                progress_callback=update_progress, workers=workers, custom_delay=custom_delay
                            )
                            results_text = "📊 **نتایج استارت رفرال:**\n\n"
                            btn_retry = "start_referral"
                            op_log_name = "bulk_referral"
                            op_log_detail = f"@{state['bot_username']} - {results['success']}/{total}"
                            
                        elif op_type == 'react':
                            results = await self.reaction_manager.bulk_react_and_view(
                                session_paths, state['channel_link'], state['message_id'], reaction_count=1,
                                progress_callback=update_progress, workers=workers, custom_delay=custom_delay
                            )
                            results_text = "📊 **نتایج ری‌آکشن:**\n\n"
                            btn_retry = "react_post"
                            op_log_name = "bulk_reaction"
                            op_log_detail = f"{state['channel_link']}/{state['message_id']} - {results['success']}/{total}"
                            
                        elif op_type == 'view_only':
                            results = await self.reaction_manager.bulk_view_only(
                                session_paths, state['channel_link'], state['message_id'],
                                progress_callback=update_progress, workers=workers, custom_delay=custom_delay
                            )
                            results_text = "📊 **نتایج سین پیام:**\n\n"
                            btn_retry = "do_view_only"
                            op_log_name = "bulk_view"
                            op_log_detail = f"{state['channel_link']}/{state['message_id']} - {results['success']}/{total}"

                        elif op_type == 'leave_all_channels':
                            results = await self.channel_manager.bulk_leave_all(
                                session_paths,
                                progress_callback=update_progress, workers=workers, custom_delay=custom_delay
                            )
                            total_left = results.get('total_left', 0)
                            results_text = f"📊 **نتایج لفت همه کانال‌ها:**\n\n🗑 کل کانال/گروه لفت شده: {total_left}\n\n"
                            btn_retry = "leave_all_channels"
                            op_log_name = "bulk_leave_all"
                            op_log_detail = f"total_left={total_left} - {results['success']}/{total}"
                            
                        if results:
                            # نمایش جزئیات نتایج
                            for i, detail in enumerate(results['details'][:10], 1):
                                phone_short = selected_accounts[i-1].phone[-4:] if selected_accounts[i-1].phone else "****"
                                result = detail['result']
                                if result['success']:
                                    results_text += f"✅ {phone_short}: موفق\n"
                                else:
                                    results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                                    
                            if len(results['details']) > 10:
                                results_text += f"\n... و {len(results['details']) - 10} مورد دیگر\n"
                                
                            results_text += f"\n✅ موفق: {results['success']}\n"
                            results_text += f"❌ ناموفق: {results['failed']}"
                            
                            # بررسی سشن‌های نامعتبر
                            invalid_count = await self._process_bulk_results_for_invalid_sessions(results, selected_accounts)
                            if invalid_count > 0:
                                results_text += f"\n⚠️ سشن نامعتبر: {invalid_count} (غیرفعال شد)"
                                
                            await progress_msg.edit(
                                results_text,
                                buttons=[
                                    [Button.inline("🔄 اجرای مجدد", btn_retry)],
                                    [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                                ]
                            )
                            
                            await self.db.log_action(op_log_name, user_id, op_log_detail)
                            
                    except Exception as e:
                        logger.exception(f"خطا در اجرای فرآیند پس‌زمینه: {e}")
                        try:
                            await progress_msg.edit(
                                f"❌ **خطا در اجرا:**\n\n{str(e)[:200]}",
                                buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                            )
                        except:
                            pass
                    finally:
                        if user_id in self.running_operations:
                            del self.running_operations[user_id]

                # تابع async برای اجرای در پس‌زمینه
                async def run_scenario_background():
                    used_sessions = set()
                    extracted_codes = {}
                    successful_sessions = []
                    
                    try:
                        # اجرای سناریو با قفل per-session و worker pool
                        results = {
                            'success': 0,
                            'failed': 0,
                            'cancelled': 0,
                            'details': [],
                            'lock': asyncio.Lock()  # برای thread-safe بودن results
                        }
                        
                        total = len(selected_accounts)
                        completed = {'count': 0}  # تعداد تکمیل شده
                        
                        # تابع worker برای اجرای هر اکانت
                        async def process_account(account, index):
                            nonlocal completed
                            
                            try:
                                # بررسی لغو عملیات
                                if cancel_flag.get('cancelled'):
                                    return
                                
                                # بررسی توقف موقت
                                while cancel_flag.get('paused'):
                                    await asyncio.sleep(1)
                                    if cancel_flag.get('cancelled'):
                                        return
                                
                                session_path = account.session_path
                                
                                # صبر تا سشن آزاد بشه (حداکثر 5 دقیقه)
                                max_wait = 300
                                wait_time = 0
                                while session_path in self.session_locks and wait_time < max_wait:
                                    await asyncio.sleep(1)
                                    wait_time += 1
                                
                                # اگر هنوز قفله، skip کن
                                if session_path in self.session_locks:
                                    async with results['lock']:
                                        results['failed'] += 1
                                        results['details'].append({
                                            'session': Path(session_path).name,
                                            'result': {
                                                'success': False,
                                                'message': 'سشن بعد از 5 دقیقه هنوز قفل بود'
                                            }
                                        })
                                    return
                                
                                # قفل کردن سشن
                                self.session_locks.add(session_path)
                                used_sessions.add(session_path)
                                
                                # بروزرسانی پیشرفت
                                try:
                                    phone_short = account.phone[-4:] if account.phone else "****"
                                    async with results['lock']:
                                        completed['count'] += 1
                                        current = completed['count']
                                    
                                    if is_multi_bot:
                                        await progress_msg.edit(
                                            f"⏳ **در حال اجرا...**\n\n"
                                            f"📊 پیشرفت: {current}/{total}\n"
                                            f"⚡ همزمان: {workers} اکانت\n"
                                            f"💬 در حال پردازش...",
                                            buttons=[
                                                [Button.inline("⏸ توقف موقت", b"pause_scenario")],
                                                [Button.inline("🛑 لغو کامل", b"cancel_scenario")]
                                            ]
                                        )
                                    else:
                                        bot_username = state['bot_username']
                                        await progress_msg.edit(
                                            f"⏳ **در حال اجرا...**\n\n"
                                            f"🤖 ربات: @{bot_username}\n"
                                            f"📊 پیشرفت: {current}/{total}\n"
                                            f"⚡ همزمان: {workers} اکانت\n"
                                            f"💬 در حال پردازش...",
                                            buttons=[
                                                [Button.inline("⏸ توقف موقت", b"pause_scenario")],
                                                [Button.inline("🛑 لغو کامل", b"cancel_scenario")]
                                            ]
                                        )
                                except:
                                    pass
                                
                                # اجرای سناریو
                                try:
                                    if is_multi_bot:
                                        bots_scenarios = state['bots_scenarios']
                                        result = await self.bot_automation.execute_multi_bot_scenario(
                                            session_path, bots_scenarios, db=self.db
                                        )
                                    else:
                                        bot_username = state['bot_username']
                                        scenario = state['scenario']
                                        
                                        parent_ref = None
                                        if is_tree_referral:
                                            idx = len(successful_sessions)
                                            parent_ref = self.bot_automation.resolve_parent_ref(scenario_text, idx, selected_accounts, extracted_codes, successful_sessions)
                                        
                                        result = await self.bot_automation.execute_scenario(
                                            session_path, bot_username, scenario, db=self.db, parent_ref=parent_ref
                                        )
                                    
                                    async with results['lock']:
                                        if result['success']:
                                            results['success'] += 1
                                            if result.get('extracted_ref_code'):
                                                extracted_codes[session_path] = result['extracted_ref_code']
                                            if result.get('is_new_user', True):
                                                successful_sessions.append(session_path)
                                        elif result.get('invalid_session'):
                                            results['failed'] += 1
                                            logger.warning(f"سشن نامعتبر غیرفعال شد: {session_path}")
                                        else:
                                            results['failed'] += 1
                                        
                                        results['details'].append({
                                            'session': Path(session_path).name,
                                            'result': result
                                        })
                                
                                finally:
                                    # آزاد کردن سشن بلافاصله بعد از استفاده
                                    self.session_locks.discard(session_path)
                                    used_sessions.discard(session_path)
                                    
                                    # ذخیره پیشرفت بعد از هر اکانت
                                    current_index = start_index + index
                                    total_accounts = len(state['active_accounts'])
                                    await self.db.save_scenario_progress(
                                        user_id, scenario_text, current_index, total_accounts
                                    )
                                
                            except Exception as e:
                                logger.error(f"خطا در worker: {e}")
                                async with results['lock']:
                                    results['failed'] += 1
                        
                        # اجرای همزمان با worker pool
                        if workers == 1:
                            # حالت تک worker (یکی یکی)
                            for index, account in enumerate(selected_accounts, 1):
                                if cancel_flag.get('cancelled'):
                                    results['cancelled'] = total - index + 1
                                    break
                                await process_account(account, index)
                                # تاخیر بین اکانت‌ها
                                if index < total and not cancel_flag.get('cancelled'):
                                    if custom_delay is not None:
                                        delay = custom_delay
                                    else:
                                        delay = Config.DELAY_BETWEEN_ACTIONS + random.randint(0, Config.DELAY_RANDOM_RANGE)
                                    await asyncio.sleep(delay)
                        else:
                            # حالت چند worker (همزمان)
                            tasks = []
                            for index, account in enumerate(selected_accounts, 1):
                                if cancel_flag.get('cancelled'):
                                    results['cancelled'] = total - index + 1
                                    break
                                task = asyncio.create_task(process_account(account, index))
                                tasks.append(task)
                                
                                # اگر تعداد task ها به حد worker رسید، صبر کن
                                if len(tasks) >= workers:
                                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                                    # حذف task های تکمیل شده
                                    tasks = [t for t in tasks if not t.done()]
                                    # تاخیر کوچک
                                    await asyncio.sleep(0.5)
                            
                            # صبر برای تکمیل همه task ها
                            if tasks:
                                await asyncio.gather(*tasks, return_exceptions=True)
                        
                        # نمایش نتایج
                        results_text = "📊 **نتایج اجرای سناریو:**\n\n"
                        if is_multi_bot:
                            bots_scenarios = state['bots_scenarios']
                            results_text += f"🤖 تعداد رباتها: {len(bots_scenarios)}\n\n"
                        else:
                            bot_username = state['bot_username']
                            results_text += f"🤖 ربات: @{bot_username}\n\n"
                        
                        # حذف flag عملیات
                        if user_id in self.running_operations:
                            del self.running_operations[user_id]
                        
                        # اگر سناریو کامل شد، پیشرفت رو پاک کن
                        if not cancel_flag.get('cancelled') and (start_index + total) >= len(state['active_accounts']):
                            await self.db.delete_scenario_progress(user_id, scenario_text)
                        
                        # ساخت فایل گزارش کامل
                        from datetime import datetime
                        import io
                        
                        report_lines = []
                        report_lines.append("=" * 60)
                        report_lines.append("گزارش اجرای سناریو پیشرفته")
                        report_lines.append("=" * 60)
                        report_lines.append(f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        report_lines.append(f"کاربر: {user_id}")
                        report_lines.append(f"تعداد اکانت‌ها: {total}")
                        report_lines.append("")
                        
                        if is_multi_bot:
                            report_lines.append(f"تعداد رباتها: {len(bots_scenarios)}")
                            for bot_data in bots_scenarios:
                                report_lines.append(f"  - @{bot_data['bot_username']}")
                        else:
                            report_lines.append(f"ربات: @{bot_username}")
                        
                        report_lines.append("")
                        report_lines.append("=" * 60)
                        report_lines.append("جزئیات اجرا برای هر اکانت")
                        report_lines.append("=" * 60)
                        report_lines.append("")
                        
                        # جزئیات هر اکانت
                        for i, detail in enumerate(results['details'], 1):
                            account = selected_accounts[i-1]
                            phone = account.phone
                            username = account.telegram_username or "ندارد"
                            result = detail['result']
                            
                            report_lines.append(f"{'=' * 60}")
                            report_lines.append(f"اکانت #{i}")
                            report_lines.append(f"{'=' * 60}")
                            report_lines.append(f"شماره: {phone}")
                            report_lines.append(f"یوزرنیم: @{username}")
                            report_lines.append(f"وضعیت کلی: {'✅ موفق' if result['success'] else '❌ ناموفق'}")
                            report_lines.append("")
                            
                            if is_multi_bot:
                                # چند ربات
                                if 'results' in result:
                                    for bot_result in result['results']:
                                        bot_name = bot_result['bot']
                                        bot_res = bot_result['result']
                                        
                                        report_lines.append(f"🤖 ربات: @{bot_name}")
                                        report_lines.append(f"   وضعیت: {'✅ موفق' if bot_res['success'] else '❌ ناموفق'}")
                                        
                                        if 'executed_steps' in bot_res:
                                            report_lines.append("   مراحل اجرا شده:")
                                            for step in bot_res['executed_steps']:
                                                report_lines.append(f"      {step}")
                                        
                                        if not bot_res['success']:
                                            report_lines.append(f"   ❌ خطا: {bot_res.get('message', 'نامشخص')}")
                                        
                                        report_lines.append("")
                            else:
                                # یک ربات
                                if 'executed_steps' in result:
                                    report_lines.append("مراحل اجرا شده:")
                                    for step in result['executed_steps']:
                                        report_lines.append(f"   {step}")
                                    report_lines.append("")
                                
                                if not result['success']:
                                    report_lines.append(f"❌ خطا: {result.get('message', 'نامشخص')}")
                                    report_lines.append("")
                            
                            report_lines.append("")
                        
                        # خلاصه نهایی
                        report_lines.append("=" * 60)
                        report_lines.append("خلاصه نتایج")
                        report_lines.append("=" * 60)
                        report_lines.append(f"✅ موفق: {results['success']}")
                        report_lines.append(f"❌ ناموفق: {results['failed']}")
                        if results.get('cancelled', 0) > 0:
                            report_lines.append(f"🛑 لغو شده: {results['cancelled']}")
                        if results.get('invalid_sessions', 0) > 0:
                            report_lines.append(f"⚠️ سشن نامعتبر (غیرفعال شد): {results['invalid_sessions']}")
                        report_lines.append("")
                        report_lines.append("=" * 60)
                        
                        # ساخت فایل با نام یونیک (شامل user_id و timestamp)
                        report_content = "\n".join(report_lines)
                        report_file = io.BytesIO(report_content.encode('utf-8'))
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        report_file.name = f"scenario_report_user{user_id}_{timestamp}.txt"
                        
                        # نمایش خلاصه در پیام
                        for i, detail in enumerate(results['details'][:5], 1):  # نمایش 5 مورد اول
                            phone_short = selected_accounts[i-1].phone[-4:] if selected_accounts[i-1].phone else "****"
                            result = detail['result']
                            
                            if result['success']:
                                results_text += f"✅ {phone_short}:\n"
                                if is_multi_bot and 'results' in result:
                                    for bot_result in result['results'][:2]:
                                        bot_name = bot_result['bot']
                                        results_text += f"   @{bot_name}: {'✅' if bot_result['result']['success'] else '❌'}\n"
                                else:
                                    for step_result in result.get('executed_steps', [])[:3]:
                                        results_text += f"   {step_result}\n"
                            else:
                                results_text += f"❌ {phone_short}: {result['message'][:30]}\n"
                            
                            results_text += "\n"
                        
                        if len(results['details']) > 5:
                            results_text += f"... و {len(results['details']) - 5} اکانت دیگر\n\n"
                        
                        results_text += f"✅ موفق: {results['success']}\n"
                        results_text += f"❌ ناموفق: {results['failed']}"
                        
                        if results.get('cancelled', 0) > 0:
                            results_text += f"\n🛑 لغو شده: {results['cancelled']}"
                        
                        if results.get('invalid_sessions', 0) > 0:
                            results_text += f"\n⚠️ سشن نامعتبر: {results['invalid_sessions']} (غیرفعال و منتقل به invalid/)"
                        
                        results_text += f"\n\n📄 **گزارش کامل در فایل ارسال شد**"
                        
                        await progress_msg.edit(
                            results_text,
                            buttons=[
                                [Button.inline("🎯 سناریو جدید", b"advanced_scenario")],
                                [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                            ]
                        )
                        
                        # ارسال فایل گزارش
                        await self.bot.send_message(
                            user_id,
                            "📄 **گزارش کامل اجرای سناریو:**",
                            file=report_file
                        )
                        
                        # سوال یادداشت برای رباتها
                        if is_multi_bot:
                            # ذخیره اطلاعات برای یادداشت
                            self.user_states[user_id] = {
                                'step': 'ask_note_multi',
                                'bots_scenarios': bots_scenarios,
                                'scenario_text': scenario_text,
                                'current_bot_index': 0
                            }
                            
                            first_bot = bots_scenarios[0]['bot_username']
                            await self.bot.send_message(
                                user_id,
                                f"📝 **یادداشت برای ربات @{first_bot}**\n\n"
                                f"آیا می‌خواهید یادداشتی برای این ربات ثبت کنید؟\n\n"
                                f"💡 یادداشت‌ها به شما کمک می‌کنند تا اطلاعات مهم درباره هر ربات را ذخیره کنید.",
                                buttons=[
                                    [Button.inline("✅ بله، یادداشت می‌زنم", b"note_yes")],
                                    [Button.inline("⏭ بعدی", b"note_skip")],
                                    [Button.inline("❌ نه، برای هیچکدام", b"note_no_all")]
                                ]
                            )
                        else:
                            # یک ربات
                            self.user_states[user_id] = {
                                'step': 'ask_note_single',
                                'bot_username': bot_username,
                                'scenario_text': scenario_text
                            }
                            
                            await self.bot.send_message(
                                user_id,
                                f"📝 **یادداشت برای ربات @{bot_username}**\n\n"
                                f"آیا می‌خواهید یادداشتی برای این ربات ثبت کنید؟\n\n"
                                f"💡 یادداشت‌ها به شما کمک می‌کنند تا اطلاعات مهم درباره ربات را ذخیره کنید.",
                                buttons=[
                                    [Button.inline("✅ بله، یادداشت می‌زنم", b"note_yes")],
                                    [Button.inline("❌ نه، نیازی نیست", b"note_no")]
                                ]
                            )
                        
                        if is_multi_bot:
                            await self.db.log_action('bulk_multi_scenario', user_id, f"{len(bots_scenarios)} bots - {results['success']}/{total}")
                            # ثبت تاریخچه برای هر ربات جداگانه
                            for bot_data in bots_scenarios:
                                await self.db.add_bot_history(
                                    user_id=user_id,
                                    bot_username=bot_data['bot_username'],
                                    accounts_total=total,
                                    accounts_success=results['success'],
                                    accounts_failed=results['failed'],
                                    scenario_text=scenario_text
                                )
                        else:
                            await self.db.log_action('bulk_scenario', user_id, f"@{bot_username} - {results['success']}/{total}")
                            # ثبت تاریخچه
                            await self.db.add_bot_history(
                                user_id=user_id,
                                bot_username=bot_username,
                                accounts_total=total,
                                accounts_success=results['success'],
                                accounts_failed=results['failed'],
                                scenario_text=scenario_text
                            )
                    
                    except Exception as e:
                        logger.exception(f"خطا در اجرای سناریو پس‌زمینه: {e}")
                        try:
                            await progress_msg.edit(
                                f"❌ **خطا در اجرای سناریو:**\n\n{str(e)[:200]}",
                                buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                            )
                        except:
                            pass
                        
                        # حذف flag عملیات
                        if user_id in self.running_operations:
                            del self.running_operations[user_id]
                        
                        # آزاد کردن سشن‌های باقی‌مانده (در صورت خطا)
                        for session_path in used_sessions:
                            self.session_locks.discard(session_path)
                
                # اجرای تسک در پس‌زمینه بر اساس نوع عملیات
                if op_type == 'scenario':
                    asyncio.create_task(run_scenario_background())
                else:
                    asyncio.create_task(run_bulk_operation_background())
                
                # پاک کردن state کاربر تا بتونه کار دیگه شروع کنه
                del self.user_states[user_id]
        

            
            elif step == 'leech_link':
                # دریافت لینک گروه
                group_link = event.message.text.strip()
                
                # ذخیره لینک
                state['group_link'] = group_link
                state['step'] = 'leech_account_count'
                
                # دریافت اکانت‌های کاربر
                accounts = await self.db.get_accounts(user_id)
                active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                
                state['active_accounts'] = active_accounts
                
                await event.respond(
                    f"📊 **انتخاب تعداد اکانت**\n\n"
                    f"🔗 گروه: `{group_link[:50]}...`\n\n"
                    f"شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                    f"چند تا اکانت برای لیچ استفاده شود؟\n\n"
                    f"💡 **گزینه‌ها:**\n"
                    f"• عدد بفرست (مثلاً `5`)\n"
                    f"• `/all` - همه اکانت‌ها",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )
            
            elif step == 'leech_account_count':
                # دریافت تعداد اکانت
                count_input = event.message.text.strip()
                
                active_accounts = state['active_accounts']
                
                # تعیین تعداد اکانت
                if count_input.lower() == '/all':
                    selected_accounts = active_accounts
                else:
                    try:
                        count = int(count_input)
                        if count <= 0:
                            await event.respond(
                                "❌ تعداد باید بیشتر از صفر باشد!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return
                        selected_accounts = active_accounts[:min(count, len(active_accounts))]
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً یک عدد معتبر وارد کنید!",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return
                
                state['selected_accounts'] = selected_accounts
                state['step'] = 'leech_target_count'
                
                await event.respond(
                    f"🎯 **تعداد اعضای مورد نظر**\n\n"
                    f"👥 تعداد اکانت: {len(selected_accounts)}\n\n"
                    f"چند عضو می‌خواهید استخراج کنید؟\n\n"
                    f"💡 **مثال:**\n"
                    f"• `100` - 100 عضو\n"
                    f"• `500` - 500 عضو\n"
                    f"• `1000` - 1000 عضو\n\n"
                    f"⚠️ **نکته:** فقط اعضای هویت‌دار (با پروفایل، نام کامل، یوزرنیم) استخراج می‌شوند.",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )
            
            elif step == 'leech_target_count':
                # دریافت تعداد اعضای مورد نظر
                try:
                    target_count = int(event.message.text.strip())

                    if target_count <= 0:
                        await event.respond(
                            "❌ تعداد باید بیشتر از صفر باشد!",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return

                    if target_count > 10000:
                        await event.respond(
                            "❌ حداکثر 10,000 پروفایل می‌توانید لیچ کنید!",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return

                    state['target_count'] = target_count

                    group_link = state['group_link']
                    selected_accounts = state['selected_accounts']

                    # ارسال پیام شروع
                    progress_msg = await event.respond(
                        f"🔄 **شروع لیچ...**\n\n"
                        f"🔗 گروه: `{group_link[:50]}`\n"
                        f"👥 تعداد اکانت: {len(selected_accounts)}\n"
                        f"🎯 هدف: {target_count} پروفایل\n\n"
                        f"⏳ لطفاً صبر کنید...\n"
                        f"💡 عکس‌ها و بیو هم دانلود می‌شوند.",
                        buttons=Button.inline("❌ لغو", b"cancel_scenario")
                    )

                    cancel_flag = {'cancelled': False}
                    self.running_operations[user_id] = cancel_flag

                    async def progress_callback(current, total, message):
                        try:
                            await progress_msg.edit(
                                f"🔄 **در حال لیچ...**\n\n"
                                f"🔗 گروه: `{group_link[:50]}`\n"
                                f"👥 اکانت: {current}/{total}\n"
                                f"🎯 هدف: {target_count} پروفایل\n\n"
                                f"📊 {message}",
                                buttons=Button.inline("❌ لغو", b"cancel_scenario")
                            )
                        except Exception as e:
                            logger.error(f"خطا در بروزرسانی پیشرفت: {e}")

                    from src.services.leecher import Leecher
                    leecher = Leecher()
                    session_paths = [acc.session_path for acc in selected_accounts]

                    results = await leecher.bulk_leech_and_save(
                        session_paths=session_paths,
                        group_link=group_link,
                        total_target=target_count,
                        owner_user_id=user_id,
                        db=self.db,
                        filter_verified=True,
                        fetch_bio=True,
                        fetch_photos=True,
                        progress_callback=progress_callback,
                        cancel_flag=cancel_flag,
                    )

                    if user_id in self.running_operations:
                        del self.running_operations[user_id]

                    if cancel_flag.get('cancelled'):
                        await progress_msg.edit(
                            f"❌ **لیچ لغو شد!**\n\n"
                            f"📊 تا اینجا {results.get('saved_count', 0)} پروفایل ذخیره شد.",
                            buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                        )
                        del self.user_states[user_id]
                        return

                    # آمار نهایی
                    stats = await self.db.get_profiles_stats(user_id)

                    invalid = results.get('invalid_sessions', 0)
                    invalid_line = f"• ⚠️ سشن نامعتبر غیرفعال شد: {invalid}\n" if invalid else ""

                    await progress_msg.edit(
                        f"✅ **لیچ تکمیل شد!**\n\n"
                        f"📊 **نتیجه این لیچ:**\n"
                        f"• ذخیره شده: {results['saved_count']}\n"
                        f"• اکانت استفاده شده: {results['accounts_used']}\n"
                        f"• اکانت ناموفق: {results['failed_accounts']}\n"
                        f"{invalid_line}"
                        f"\n🗄 **کل پروفایل‌های شما در دیتابیس:**\n"
                        f"• کل: {stats['total']}\n"
                        f"• استفاده نشده: {stats['unused']}\n"
                        f"• استفاده شده: {stats['used']}\n\n"
                        f"💡 برای اعمال روی اکانت‌ها از **🎨 اعمال پروفایل** استفاده کنید.",
                        buttons=[
                            [Button.inline("🎨 اعمال پروفایل", b"apply_profiles")],
                            [Button.inline("👥 لیچ جدید", b"leecher")],
                            [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                        ]
                    )

                    self.user_states.pop(user_id, None)

                except ValueError:
                    await event.respond(
                        "❌ لطفاً یک عدد معتبر وارد کنید!",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                except Exception as e:
                    logger.exception(f"خطا در لیچ: {e}")
                    await event.respond(
                        f"❌ **خطا در لیچ!**\n\nخطا: {str(e)[:100]}",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                    self.user_states.pop(user_id, None)
                    self.running_operations.pop(user_id, None)
            
            elif step == 'apply_excel_file':
                # دریافت فایل Excel
                if not event.message.document:
                    await event.respond(
                        "❌ لطفاً یک فایل Excel ارسال کنید!",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    return
                
                # بررسی نوع فایل
                file_name = event.message.document.attributes[0].file_name if event.message.document.attributes else "file"
                
                if not (file_name.endswith('.xlsx') or file_name.endswith('.xls')):
                    await event.respond(
                        "❌ فقط فایل‌های Excel (.xlsx یا .xls) پذیرفته می‌شوند!",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                    return
                
                try:
                    # دانلود فایل
                    progress_msg = await event.respond("⏳ در حال دانلود فایل...")
                    
                    file_path = Path('data/temp_profiles.xlsx')
                    file_path.parent.mkdir(exist_ok=True)
                    
                    await event.message.download_media(str(file_path))
                    
                    # خواندن فایل Excel
                    import pandas as pd
                    df = pd.read_excel(file_path)
                    
                    # تبدیل به لیست دیکشنری
                    members_data = []
                    for _, row in df.iterrows():
                        member = {
                            'user_id': row.get('User ID'),
                            'username': row.get('Username', '').replace('@', '').replace('ندارد', ''),
                            'first_name': row.get('First Name', 'User'),
                            'last_name': row.get('Last Name', ''),
                            'phone': row.get('Phone', ''),
                            'has_profile': row.get('Has Profile', 'خیر') == 'بله',
                            'access_hash': row.get('Access Hash')
                        }
                        members_data.append(member)
                    
                    # حذف فایل موقت
                    file_path.unlink()
                    
                    # ذخیره اطلاعات
                    state['members_data'] = members_data
                    state['step'] = 'apply_account_count'
                    
                    # دریافت اکانت‌های کاربر
                    accounts = await self.db.get_accounts(user_id)
                    active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]
                    
                    state['active_accounts'] = active_accounts
                    
                    await progress_msg.edit(
                        f"✅ **فایل دریافت شد!**\n\n"
                        f"📊 تعداد پروفایل: {len(members_data)}\n"
                        f"👥 اکانت‌های فعال شما: {len(active_accounts)}\n\n"
                        f"چند تا اکانت برای اعمال پروفایل استفاده شود؟\n\n"
                        f"💡 **گزینه‌ها:**\n"
                        f"• عدد بفرست (مثلاً `5`)\n"
                        f"• `/all` - همه اکانت‌ها",
                        buttons=Button.inline("❌ لغو", b"cancel")
                    )
                
                except Exception as e:
                    logger.exception(f"خطا در خواندن فایل: {e}")
                    await event.respond(
                        f"❌ **خطا در خواندن فایل!**\n\n"
                        f"خطا: {str(e)[:100]}\n\n"
                        f"لطفاً مطمئن شوید فایل Excel معتبر است.",
                        buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                    )
                    
                    if user_id in self.user_states:
                        del self.user_states[user_id]
            
            elif step == 'apply_account_count':
                # دریافت تعداد اکانت برای اعمال
                count_input = event.message.text.strip()

                active_accounts = state['active_accounts']

                # تعیین تعداد اکانت
                if count_input.lower() == '/all':
                    selected_accounts = active_accounts
                else:
                    try:
                        count = int(count_input)
                        if count <= 0:
                            await event.respond(
                                "❌ تعداد باید بیشتر از صفر باشد!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return

                        if count > len(active_accounts):
                            await event.respond(
                                f"❌ شما فقط {len(active_accounts)} اکانت فعال دارید!",
                                buttons=Button.inline("❌ لغو", b"cancel")
                            )
                            return

                        selected_accounts = active_accounts[:count]
                    except ValueError:
                        await event.respond(
                            "❌ لطفاً یک عدد معتبر وارد کنید یا `/all` بفرستید!",
                            buttons=Button.inline("❌ لغو", b"cancel")
                        )
                        return

                state['selected_accounts'] = selected_accounts
                state['step'] = 'apply_options'

                # آمار پروفایل‌های موجود
                stats = await self.db.get_profiles_stats(user_id)

                await event.respond(
                    f"🎨 **تنظیمات اعمال پروفایل**\n\n"
                    f"👥 تعداد اکانت انتخاب شده: {len(selected_accounts)}\n"
                    f"🗄 پروفایل‌های استفاده‌نشده شما: {stats['unused']}\n\n"
                    f"چه مواردی اعمال شود؟\n\n"
                    f"✅ نام و نام خانوادگی (همیشه)\n"
                    f"🖼 عکس پروفایل (تا 5 عکس ذخیره شده)\n"
                    f"📝 بیو\n"
                    f"🔤 یوزرنیم (با پسوند رندوم)\n\n"
                    f"گزینه مورد نظر را انتخاب کنید:",
                    buttons=[
                        [Button.inline("🖼 نام + عکس", b"apply_opt_name_photo")],
                        [Button.inline("📝 نام + بیو", b"apply_opt_name_bio")],
                        [Button.inline("🎨 نام + عکس + بیو", b"apply_opt_name_photo_bio")],
                        [Button.inline("🔤 همه (با یوزرنیم)", b"apply_opt_all")],
                        [Button.inline("❌ لغو", b"cancel")]
                    ]
                )

        @self.bot.on(events.CallbackQuery(pattern=b"skip_time_limit"))
        async def skip_time_limit_callback(event):
            """رد کردن محدودیت زمانی"""
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                await event.answer("⚠️ لطفاً دوباره عملیات را شروع کنید", alert=True)
                return
            
            await event.answer()  # بستن notification
            
            state = self.user_states[user_id]
            current_step = state.get('step', '')
            
            # فقط برای scenario_time_limit کار می‌کنه
            if current_step == 'scenario_time_limit':
                # شبیه‌سازی ارسال /skip
                # ساخت یک event جعلی با متن /skip
                class FakeMessage:
                    def __init__(self):
                        self.text = '/skip'
                
                class FakeEvent:
                    def __init__(self, original_event):
                        self.sender_id = original_event.sender_id
                        self.message = FakeMessage()
                        self._original = original_event
                    
                    async def respond(self, *args, **kwargs):
                        return await self._original.respond(*args, **kwargs)
                
                fake_event = FakeEvent(event)
                
                # فراخوانی handler اصلی با event جعلی
                # این کار باعث میشه کد scenario_time_limit اجرا بشه
                await message_handler(fake_event)
            else:
                await event.answer("❌ این دکمه فقط برای سناریو کار می‌کند", alert=True)
        
        @self.bot.on(events.CallbackQuery(pattern=b"cancel_scenario"))
        async def cancel_scenario_callback(event):
            """لغو عملیات سناریو در حال اجرا"""
            user_id = event.sender_id
            
            if user_id in self.running_operations:
                # تنظیم flag لغو
                self.running_operations[user_id]['cancelled'] = True
                await event.answer("🛑 در حال لغو عملیات...", alert=True)
                
                # ویرایش پیام
                try:
                    await event.edit(
                        event.message.text + "\n\n🛑 **درخواست لغو دریافت شد...**",
                        buttons=None
                    )
                except:
                    pass
            else:
                await event.answer("⚠️ عملیاتی در حال اجرا نیست", alert=True)
        
        @self.bot.on(events.CallbackQuery(pattern=b"pause_scenario"))
        async def pause_scenario_callback(event):
            """توقف موقت سناریو"""
            user_id = event.sender_id
            
            if user_id in self.running_operations:
                # تنظیم flag توقف
                self.running_operations[user_id]['paused'] = True
                await event.answer("⏸ سناریو متوقف شد", alert=True)
                
                # ویرایش پیام و تغییر دکمه
                try:
                    await event.edit(
                        event.message.text.replace("⏳ **در حال اجرا...**", "⏸ **متوقف شده**"),
                        buttons=[
                            [Button.inline("▶️ ادامه", b"resume_scenario_run")],
                            [Button.inline("🛑 لغو کامل", b"cancel_scenario")]
                        ]
                    )
                except:
                    pass
            else:
                await event.answer("⚠️ عملیاتی در حال اجرا نیست", alert=True)
        
        @self.bot.on(events.CallbackQuery(pattern=b"resume_scenario_run"))
        async def resume_scenario_run_callback(event):
            """ادامه سناریو بعد از توقف"""
            user_id = event.sender_id
            
            if user_id in self.running_operations:
                # غیرفعال کردن flag توقف
                self.running_operations[user_id]['paused'] = False
                await event.answer("▶️ سناریو ادامه یافت", alert=True)
                
                # ویرایش پیام و تغییر دکمه
                try:
                    await event.edit(
                        event.message.text.replace("⏸ **متوقف شده**", "⏳ **در حال اجرا...**"),
                        buttons=[
                            [Button.inline("⏸ توقف موقت", b"pause_scenario")],
                            [Button.inline("🛑 لغو کامل", b"cancel_scenario")]
                        ]
                    )
                except:
                    pass
            else:
                await event.answer("⚠️ عملیاتی در حال اجرا نیست", alert=True)
        
        @self.bot.on(events.CallbackQuery(pattern=b"admin_set_backup_channel"))
        async def admin_set_backup_channel_callback(event):
            """تنظیم کانال بکاپ"""
            # فقط سازنده دسترسی داره
            if not await self._check_creator_access(event):
                return
            
            await event.answer()
            await event.edit(
                "⚙️ **تنظیم کانال بکاپ**\n\n"
                "آیدی عددی کانال بکاپ را ارسال کنید.\n\n"
                "💡 **نکته:** ربات باید ادمین کانال باشد.\n\n"
                "برای دریافت آیدی کانال:\n"
                "1. پیام از کانال فوروارد کنید به @userinfobot\n"
                "2. آیدی عددی را کپی کنید (مثل: -1001234567890)",
                buttons=Button.inline("❌ لغو", b"admin_panel")
            )
            self.user_states[event.sender_id] = {'step': 'set_backup_channel'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"admin_backup"))
        async def admin_backup_callback(event):
            """بکاپ کامل سیستم"""
            # فقط سازنده دسترسی داره
            if not await self._check_creator_access(event):
                return
            
            await event.answer()
            
            if not self.backup_manager.backup_channel_id:
                await event.edit(
                    "⚠️ **کانال بکاپ تنظیم نشده است!**\n\n"
                    "ابتدا کانال بکاپ را تنظیم کنید.",
                    buttons=[
                        [Button.inline("⚙️ تنظیم کانال", b"admin_set_backup_channel")],
                        [Button.inline("🔙 بازگشت", b"admin_panel")]
                    ]
                )
                return
            
            progress_msg = await event.edit(
                "⏳ **در حال بکاپ گیری...**\n\n"
                "لطفاً صبر کنید..."
            )
            
            try:
                # بکاپ دیتابیس
                await progress_msg.edit(
                    "⏳ **در حال بکاپ دیتابیس...**\n\n"
                    "📊 مرحله 1 از 3"
                )
                
                db_result = await self.backup_manager.backup_database(Config.DATABASE_PATH)
                
                if not db_result['success']:
                    await progress_msg.edit(
                        f"❌ خطا در بکاپ دیتابیس:\n{db_result['message']}",
                        buttons=Button.inline("🔙 بازگشت", b"admin_panel")
                    )
                    return
                
                # آپلود دیتابیس به کانال
                await progress_msg.edit(
                    "⏳ **در حال آپلود دیتابیس...**\n\n"
                    "📊 مرحله 2 از 3"
                )
                
                upload_db_result = await self.backup_manager.upload_database_backup(
                    db_result['backup_path']
                )
                
                # بکاپ سشن‌ها (زیپ شده)
                await progress_msg.edit(
                    "⏳ **در حال بکاپ سشن‌ها...**\n\n"
                    "📊 مرحله 3 از 3"
                )
                
                accounts = await self.db.get_accounts()
                session_paths = [acc.session_path for acc in accounts if acc.session_path and Path(acc.session_path).exists()]
                
                if session_paths:
                    # ساخت فایل زیپ
                    zip_result = await self.backup_manager.create_sessions_zip(session_paths)
                    
                    if zip_result['success']:
                        # آپلود فایل زیپ به کانال
                        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        caption = (
                            f"📦 **بکاپ سشن‌ها (زیپ شده)**\n\n"
                            f"📅 تاریخ: {timestamp}\n"
                            f"📱 تعداد سشن‌ها: {zip_result['total_sessions']}\n"
                            f"📁 فایل: {zip_result['zip_filename']}"
                        )
                        
                        await self.bot.send_file(
                            self.backup_manager.backup_channel_id,
                            zip_result['zip_path'],
                            caption=caption
                        )
                        
                        # حذف فایل زیپ موقت
                        Path(zip_result['zip_path']).unlink()
                        
                        sessions_status = f"✅ {zip_result['total_sessions']} سشن در فایل زیپ"
                    else:
                        sessions_status = f"❌ خطا در زیپ کردن"
                else:
                    sessions_status = "⚠️ سشنی یافت نشد"
                
                # نمایش نتیجه
                result_text = "✅ **بکاپ کامل انجام شد!**\n\n"
                result_text += f"💾 دیتابیس: {'✅ موفق' if upload_db_result['success'] else '❌ ناموفق'}\n"
                result_text += f"📦 سشن‌ها: {sessions_status}\n"
                result_text += f"📊 کل اکانت‌ها: {len(accounts)}\n\n"
                result_text += f"📅 تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                
                await progress_msg.edit(
                    result_text,
                    buttons=Button.inline("🔙 بازگشت", b"admin_panel")
                )
                
                await self.db.log_action('full_backup', event.sender_id, f"{len(session_paths)}/{len(accounts)}")
                
            except Exception as e:
                logger.exception(f"خطا در بکاپ: {e}")
                await progress_msg.edit(
                    f"❌ **خطا در بکاپ:**\n{str(e)}",
                    buttons=Button.inline("🔙 بازگشت", b"admin_panel")
                )
        
        @self.bot.on(events.CallbackQuery(pattern=b"admin_restore"))
        async def admin_restore_callback(event):
            """ریستور بکاپ"""
            # فقط سازنده دسترسی داره
            if not await self._check_creator_access(event):
                return
            
            await event.answer()
            await event.edit(
                "📥 **ریستور بکاپ**\n\n"
                "فایل بکاپ دیتابیس (.db) را ارسال کنید.\n\n"
                "⚠️ **هشدار:** دیتابیس فعلی جایگزین خواهد شد!\n"
                "یک بکاپ امنیتی قبل از ریستور ساخته می‌شود.",
                buttons=Button.inline("❌ لغو", b"admin_panel")
            )
            self.user_states[event.sender_id] = {'step': 'restore_backup'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"advanced_scenario"))
        async def advanced_scenario_callback(event):
            """شروع فرآیند سناریو پیشرفته"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            accounts = await self.db.get_accounts(event.sender_id)
            
            if not accounts:
                await event.edit(
                    "❌ شما هنوز اکانتی اضافه نکرده‌اید.\n"
                    "ابتدا یک اکانت اضافه کنید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            await event.edit(
                "🎯 **سناریو پیشرفته ربات**\n\n"
                "با این قابلیت می‌توانید یک یا چند ربات را با سناریوهای مختلف اجرا کنید.\n\n"
                "📝 **فرمت یک ربات:**\n"
                "```\n"
                "@bot_username\n"
                "start: ref_id\n"
                "send: متن\n"
                "solve_captcha: send\n"
                "click: #0\n"
                "share_phone:\n"
                "stop: 5\n"
                "forward: \"لینک\", @channel\n"
                "```\n\n"
                "📝 **فرمت چند ربات:**\n"
                "```\n"
                "@bot1\n"
                "start: ref1\n"
                "send: text1\n"
                "\n"
                "@bot2\n"
                "start: ref2\n"
                "click: #0\n"
                "```\n\n"
                "🎬 **مثال چند ربات:**\n"
                "```\n"
                "@PacketNetEmergencyBot\n"
                "start: ref_123\n"
                "join_addlist: https://t.me/addlist/BJ1gpHd43ew2MzQx\n"
                "click: عضو شدم\n"
                "solve_captcha: send\n"
                "share_phone:\n"
                "stop: 3\n"
                "leave_addlist: https://t.me/addlist/BJ1gpHd43ew2MzQx\n"
                "\n"
                "@NullVIPBot\n"
                "start: ref_456\n"
                "join: https://t.me/NullNetwork\n"
                "send: /start\n"
                "solve_captcha: click\n"
                "share_phone:\n"
                "leave: https://t.me/NullNetwork\n"
                "```\n\n"
                "📋 **دستورات** (روی هر کدام کلیک کنید تا کپی شود):\n\n"
                "`start: ref_id`\n"
                "`send: متن پیام`\n"
                "`click: متن دکمه`  یا  `click: #0`\n"
                "`auto_join`\n"
                "`auto_join: no_click`\n"
                "`auto_join_leave`  یا  `ajl`\n"
                "`auto_join_leave: no_click`\n"
                "`join: https://t.me/channel`\n"
                "`leave: https://t.me/channel`\n"
                "`jl: https://t.me/channel`\n"
                "`join_addlist: https://t.me/addlist/...`\n"
                "`leave_addlist: https://t.me/addlist/...`\n"
                "`solve_captcha: send`\n"
                "`solve_captcha: click`\n"
                "`solve_captcha: send, 3`\n"
                "`share_phone:`\n"
                "`forward: 3, @channel`\n"
                "`wait: 5`\n"
                "`stop: 5`\n"
                "`wait_for: 30`\n"
                "`wait_for: 30, کیف پول`\n"
                "`wait_for: 30, button`\n"
                "`smart_delay: on`\n"
                "`smart_delay: on, 30`\n"
                "`smart_delay: off`\n"
                "`{random:8}`  یا  `{random_upper:6}`  یا  `{random_num:4}`\n\n"
                "🎯 **انتخاب اکانت‌ها:**\n"
                "• `5` - 5 اکانت اول\n"
                "• `/all` - همه اکانت‌ها\n"
                "• `/from 70` - از اکانت 70 تا آخر\n"
                "• `/from 70 to 100` - فقط 70 تا 100\n\n"
                "⏸ **کنترل اجرا:**\n"
                "• دکمه توقف موقت در حین اجرا\n"
                "• ادامه خودکار از جایی که متوقف شده\n"
                "• گزارش کامل در فایل .txt\n\n"
                "💡 **نکات:**\n"
                "• هر ربات با @ شروع می‌شود\n"
                "• بین رباتها یک خط خالی بگذارید\n"
                "• همه رباتها برای هر اکانت اجرا می‌شوند\n"
                "• فوروارد هوشمند: فقط پیام حاوی متن مشخص\n"
                "• کپچا: خودکار معادلات +، -، ×، ÷ رو حل می‌کنه\n\n"
                "حالا سناریو خودت رو بفرست:",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'scenario_input'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"scheduled_scenario"))
        async def scheduled_scenario_callback(event):
            """منوی سناریو زمانبندی شده"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id
            
            schedules = await self.db.get_user_scheduled_scenarios(user_id)
            active = [s for s in schedules if s['is_active']]
            
            text = "⏰ **سناریو زمانبندی شده**\n\n"
            text += "سناریوی دلخواه رو یک بار تعریف کن و بگو هر چند دقیقه تکرار بشه.\n\n"
            
            if active:
                text += f"📋 **سناریوهای فعال: {len(active)}**\n\n"
                for s in active[:5]:
                    text += f"• `#{s['id']}` **{s['name']}** — هر {s['interval_minutes']} دقیقه\n"
                    if s['last_run_at']:
                        text += f"  آخرین اجرا: {s['last_run_at'][:16]}\n"
            else:
                text += "هیچ سناریوی فعالی ندارید.\n"
            
            buttons = [
                [Button.inline("➕ سناریو جدید", b"add_scheduled_scenario")],
            ]
            if active:
                buttons.append([Button.inline("📋 مدیریت سناریوها", b"manage_schedules")])
            buttons.append([Button.inline("🔙 منوی اصلی", b"back_to_menu")])
            
            await event.edit(text, buttons=buttons)

        @self.bot.on(events.CallbackQuery(pattern=b"add_scheduled_scenario"))
        async def add_scheduled_scenario_callback(event):
            """شروع فرآیند افزودن سناریو زمانبندی"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id
            
            await event.edit(
                "⏰ **سناریو زمانبندی جدید — مرحله ۱/۴**\n\n"
                "یک نام برای این سناریو بنویس:\n"
                "(مثلاً: رفرال روزانه، استارت شبانه)",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[user_id] = {'step': 'sched_name'}

        @self.bot.on(events.CallbackQuery(pattern=b"manage_schedules"))
        async def manage_schedules_callback(event):
            """مدیریت سناریوهای زمانبندی"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id
            
            schedules = await self.db.get_user_scheduled_scenarios(user_id)
            if not schedules:
                await event.edit(
                    "📋 هیچ سناریویی ندارید.",
                    buttons=Button.inline("🔙 برگشت", b"scheduled_scenario")
                )
                return
            
            text = "📋 **مدیریت سناریوهای زمانبندی**\n\n"
            buttons = []
            for s in schedules:
                status = "✅" if s['is_active'] else "⏹"
                text += f"{status} `#{s['id']}` **{s['name']}** — هر {s['interval_minutes']} دقیقه\n"
                if s['is_active']:
                    buttons.append([
                        Button.inline(f"⏹ توقف #{s['id']} ({s['name']})", f"stop_sched_{s['id']}".encode())
                    ])
                buttons.append([
                    Button.inline(f"🗑 حذف #{s['id']} ({s['name']})", f"del_sched_{s['id']}".encode())
                ])
            
            buttons.append([Button.inline("🔙 برگشت", b"scheduled_scenario")])
            await event.edit(text, buttons=buttons)

        @self.bot.on(events.CallbackQuery(pattern=rb"stop_sched_(\d+)"))
        async def stop_schedule_callback(event):
            """توقف سناریو زمانبندی"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            schedule_id = int(event.pattern_match.group(1))
            
            await self.db.deactivate_scheduled_scenario(schedule_id)
            
            # لغو task در حال اجرا
            task = self.scheduled_tasks.pop(schedule_id, None)
            if task:
                task.cancel()
            
            await event.edit(
                f"⏹ **سناریو #{schedule_id} متوقف شد.**",
                buttons=Button.inline("🔙 برگشت", b"manage_schedules")
            )

        @self.bot.on(events.CallbackQuery(pattern=rb"del_sched_(\d+)"))
        async def delete_schedule_callback(event):
            """حذف سناریو زمانبندی"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            schedule_id = int(event.pattern_match.group(1))
            
            # لغو task در حال اجرا
            task = self.scheduled_tasks.pop(schedule_id, None)
            if task:
                task.cancel()
            
            await self.db.delete_scheduled_scenario(schedule_id)
            
            await event.edit(
                f"🗑 **سناریو #{schedule_id} حذف شد.**",
                buttons=Button.inline("🔙 برگشت", b"manage_schedules")
            )

        @self.bot.on(events.CallbackQuery(pattern=b"leecher"))
        async def leecher_callback(event):
            """شروع فرآیند لیچر"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            accounts = await self.db.get_accounts(event.sender_id)
            
            if not accounts:
                await event.edit(
                    "❌ شما هنوز اکانتی اضافه نکرده‌اید.\n"
                    "ابتدا یک اکانت اضافه کنید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return
            
            await event.edit(
                "👥 **لیچر - استخراج اعضای گروه**\n\n"
                "با این قابلیت می‌توانید اعضای یک گروه را استخراج کنید.\n\n"
                "🎯 **ویژگی‌ها:**\n"
                "• استخراج اعضای هویت‌دار (با پروفایل، نام کامل، یوزرنیم، بیو)\n"
                "• استفاده همزمان از چند اکانت\n"
                "• جلوگیری از تکراری\n"
                "• تقسیم هوشمند بین اکانت‌ها\n"
                "• ذخیره اطلاعات کامل اعضا\n\n"
                "📋 **مراحل:**\n"
                "1️⃣ لینک گروه را ارسال کنید\n"
                "2️⃣ تعداد اکانت برای لیچ را مشخص کنید\n"
                "3️⃣ تعداد اعضای مورد نظر را وارد کنید\n"
                "4️⃣ منتظر بمانید تا لیچ تکمیل شود\n\n"
                "💡 **نکات:**\n"
                "• فقط اعضای هویت‌دار استخراج می‌شوند\n"
                "• اعضای تکراری حذف می‌شوند\n"
                "• نتیجه در فایل Excel ذخیره می‌شود\n\n"
                "🔗 **حالا لینک گروه را ارسال کنید:**\n"
                "(مثال: https://t.me/groupname یا @groupname)",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {'step': 'leech_link'}
        
        @self.bot.on(events.CallbackQuery(pattern=b"apply_profiles"))
        async def apply_profiles_callback(event):
            """شروع فرآیند اعمال پروفایل از دیتابیس"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return

            await event.answer()

            accounts = await self.db.get_accounts(event.sender_id)
            active_accounts = [acc for acc in accounts if acc.status == 'active' and acc.session_path]

            if not active_accounts:
                await event.edit(
                    "❌ شما هنوز اکانت فعالی ندارید.\n"
                    "ابتدا یک اکانت اضافه کنید.",
                    buttons=Button.inline("➕ افزودن اکانت", b"add_account")
                )
                return

            # آمار پروفایل‌های موجود در دیتابیس
            stats = await self.db.get_profiles_stats(event.sender_id)

            if stats['unused'] == 0:
                await event.edit(
                    "❌ **هیچ پروفایل استفاده‌نشده‌ای ندارید!**\n\n"
                    f"📊 وضعیت پروفایل‌های شما:\n"
                    f"• کل: {stats['total']}\n"
                    f"• استفاده شده: {stats['used']}\n"
                    f"• استفاده نشده: {stats['unused']}\n\n"
                    f"💡 ابتدا از **👥 لیچر** پروفایل جمع‌آوری کنید.",
                    buttons=[
                        [Button.inline("👥 لیچر", b"leecher")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
                return

            await event.edit(
                f"🎨 **اعمال پروفایل روی اکانت‌ها**\n\n"
                f"📊 **پروفایل‌های شما در دیتابیس:**\n"
                f"• استفاده نشده: {stats['unused']}\n"
                f"• استفاده شده: {stats['used']}\n"
                f"• کل: {stats['total']}\n\n"
                f"👥 اکانت‌های فعال شما: {len(active_accounts)}\n\n"
                f"📋 **مراحل:**\n"
                f"1️⃣ تعداد اکانت را مشخص کنید\n"
                f"2️⃣ نوع اطلاعات مورد نظر را انتخاب کنید\n"
                f"3️⃣ منتظر بمانید\n\n"
                f"💡 هر اکانت یک پروفایل یونیک از دیتابیس می‌گیرد.\n\n"
                f"چند تا اکانت برای اعمال استفاده شود؟\n"
                f"• عدد بفرست یا `/all` برای همه",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
            self.user_states[event.sender_id] = {
                'step': 'apply_account_count',
                'active_accounts': active_accounts,
            }
        
        @self.bot.on(events.CallbackQuery(pattern=b"apply_opt_name_photo"))
        async def apply_opt_name_photo_callback(event):
            """اعمال نام + عکس"""
            await self._apply_from_excel_handler(event, apply_photo=True, apply_bio=False, apply_username=False)
        
        @self.bot.on(events.CallbackQuery(pattern=b"apply_opt_name_bio"))
        async def apply_opt_name_bio_callback(event):
            """اعمال نام + بیو"""
            await self._apply_from_excel_handler(event, apply_photo=False, apply_bio=True, apply_username=False)
        
        @self.bot.on(events.CallbackQuery(pattern=b"apply_opt_name_photo_bio"))
        async def apply_opt_name_photo_bio_callback(event):
            """اعمال نام + عکس + بیو"""
            await self._apply_from_excel_handler(event, apply_photo=True, apply_bio=True, apply_username=False)
        
        @self.bot.on(events.CallbackQuery(pattern=b"apply_opt_all"))
        async def apply_opt_all_callback(event):
            """اعمال همه (با یوزرنیم)"""
            await self._apply_from_excel_handler(event, apply_photo=True, apply_bio=True, apply_username=True)

        @self.bot.on(events.CallbackQuery(pattern=b"apply_report_txt"))
        async def apply_report_txt_callback(event):
            """ارسال گزارش کامل اعمال پروفایل به صورت TXT"""
            if not await self._check_admin_access(event):
                return
            await event.answer("⏳ در حال آماده‌سازی فایل...", alert=False)

            user_id = event.sender_id
            state = self.user_states.get(user_id, {})
            details = state.get('apply_details', [])

            if not details:
                await event.answer("❌ اطلاعاتی یافت نشد!", alert=True)
                return

            lines = []
            lines.append("=" * 55)
            lines.append("🎨 گزارش اعمال پروفایل")
            lines.append(f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            lines.append(f"تعداد کل: {len(details)}")
            ok = sum(1 for d in details if d['status'] == 'ok')
            fail = len(details) - ok
            lines.append(f"موفق: {ok}  |  ناموفق: {fail}")
            lines.append("=" * 55)
            lines.append("")

            for i, d in enumerate(details, 1):
                icon = "✅" if d['status'] == 'ok' else "❌"
                lines.append(f"{icon} [{i}] شماره ما    : {d['acc_phone']}")
                lines.append(f"     آیدی ما     : {d['acc_id']}")
                lines.append(f"     یوزرنیم ما  : @{d['acc_user']}" if d['acc_user'] != '—' else f"     یوزرنیم ما  : —")
                lines.append(f"     آیدی منبع   : {d['src_id']}")
                lines.append(f"     نام جدید    : {d['new_name']}")
                if d['new_user'] != '—':
                    lines.append(f"     یوزرنیم جدید: @{d['new_user']}")
                if d['bio']:
                    lines.append(f"     بیو          : {d['bio']}")
                lines.append(f"     اعمال شده   : {d['applied']}")
                lines.append("")

            import io as _io
            content = "\n".join(lines)
            file_bytes = _io.BytesIO(content.encode("utf-8"))
            file_bytes.name = f"apply_report_{user_id}.txt"

            await self.bot.send_file(
                event.chat_id,
                file_bytes,
                caption=f"📄 **گزارش کامل اعمال پروفایل**\n✅ {ok} موفق  ❌ {fail} ناموفق",
                attributes=[],
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"all_countries"))
        async def all_countries_callback(event):
            """انتخاب همه کشورها"""
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                await event.answer("❌ خطا! لطفاً دوباره تلاش کنید.", alert=True)
                return
            
            await event.answer()
            
            state = self.user_states[user_id]
            
            # استفاده از همه اکانت‌ها (بدون فیلتر کشور)
            state['selected_country'] = None
            filtered_accounts = state['active_accounts']
            state['step'] = 'scenario_count'
            
            # بررسی پیشرفت قبلی
            scenario_text = state.get('scenario_text')
            scenario_summary = state.get('scenario_summary')
            progress = await self.db.get_scenario_progress(user_id, scenario_text)
            
            if progress and progress['last_account_index'] > 0:
                # سناریو قبلاً شروع شده
                last_index = progress['last_account_index']
                total = progress['total_accounts']
                
                buttons = [
                    [Button.inline(f"▶️ ادامه از اکانت {last_index + 1}", b"resume_scenario")],
                    [Button.inline("🔄 شروع از اول", b"restart_scenario")],
                    [Button.inline("🎯 انتخاب دستی", b"manual_select_scenario")],
                    [Button.inline("❌ لغو", b"cancel")]
                ]
                
                await event.edit(
                    f"⚠️ **سناریو قبلاً شروع شده!**\n\n"
                    f"{scenario_summary}\n"
                    f"📊 پیشرفت قبلی: {last_index}/{total} اکانت\n\n"
                    f"می‌خواهید از کجا ادامه دهید?",
                    buttons=buttons
                )
            else:
                # سناریو جدید
                await event.edit(
                    f"📊 **انتخاب تعداد اکانت**\n\n"
                    f"{scenario_summary}\n"
                    f"🌍 کشور: همه کشورها\n"
                    f"شما {len(filtered_accounts)} اکانت فعال دارید.\n\n"
                    f"چند تا اکانت برای اجرای سناریو استفاده شود؟\n\n"
                    f"💡 **گزینه‌ها:**\n"
                    f"• عدد بفرست (مثلاً `5`) - از اول شروع میشه\n"
                    f"• `/all` - همه اکانت‌ها\n"
                    f"• `/from 70` - از اکانت 70 شروع کن\n"
                    f"• `/from 70 to 100` - از 70 تا 100",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )
        
        @self.bot.on(events.CallbackQuery(pattern=b"country_"))
        async def country_select_callback(event):
            """انتخاب کشور خاص"""
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                await event.answer("❌ خطا! لطفاً دوباره تلاش کنید.", alert=True)
                return
            
            # استخراج کد کشور از callback data
            country_code = event.data.decode().replace('country_', '')
            
            await event.answer()
            
            state = self.user_states[user_id]
            
            # فیلتر اکانت‌ها بر اساس کشور
            state['selected_country'] = country_code
            filtered_accounts = [acc for acc in state['active_accounts'] if acc.country_code == country_code]
            
            if not filtered_accounts:
                from src.utils.countries import get_country_name
                await event.answer(
                    f"❌ هیچ اکانتی از {get_country_name(country_code)} پیدا نشد!",
                    alert=True
                )
                return
            
            # بروزرسانی لیست اکانت‌های فعال
            state['active_accounts'] = filtered_accounts
            state['step'] = 'scenario_count'
            
            # بررسی پیشرفت قبلی
            scenario_text = state.get('scenario_text')
            scenario_summary = state.get('scenario_summary')
            progress = await self.db.get_scenario_progress(user_id, scenario_text)
            
            if progress and progress['last_account_index'] > 0:
                # سناریو قبلاً شروع شده
                last_index = progress['last_account_index']
                total = progress['total_accounts']
                
                buttons = [
                    [Button.inline(f"▶️ ادامه از اکانت {last_index + 1}", b"resume_scenario")],
                    [Button.inline("🔄 شروع از اول", b"restart_scenario")],
                    [Button.inline("🎯 انتخاب دستی", b"manual_select_scenario")],
                    [Button.inline("❌ لغو", b"cancel")]
                ]
                
                from src.utils.countries import get_country_name
                await event.edit(
                    f"⚠️ **سناریو قبلاً شروع شده!**\n\n"
                    f"{scenario_summary}\n"
                    f"🌍 کشور: {get_country_name(country_code)}\n"
                    f"📊 پیشرفت قبلی: {last_index}/{total} اکانت\n\n"
                    f"می‌خواهید از کجا ادامه دهید?",
                    buttons=buttons
                )
            else:
                # سناریو جدید
                from src.utils.countries import get_country_name
                await event.edit(
                    f"📊 **انتخاب تعداد اکانت**\n\n"
                    f"{scenario_summary}\n"
                    f"🌍 کشور: {get_country_name(country_code)}\n"
                    f"شما {len(filtered_accounts)} اکانت فعال دارید.\n\n"
                    f"چند تا اکانت برای اجرای سناریو استفاده شود؟\n\n"
                    f"💡 **گزینه‌ها:**\n"
                    f"• عدد بفرست (مثلاً `5`) - از اول شروع میشه\n"
                    f"• `/all` - همه اکانت‌ها\n"
                    f"• `/from 70` - از اکانت 70 شروع کن\n"
                    f"• `/from 70 to 100` - از 70 تا 100",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )
        
        @self.bot.on(events.CallbackQuery(pattern=b"resume_scenario"))
        async def resume_scenario_callback(event):
            """ادامه سناریو از جایی که متوقف شده"""
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                await event.answer("❌ خطا! لطفاً دوباره تلاش کنید.", alert=True)
                return
            
            await event.answer()
            
            state = self.user_states[user_id]
            scenario_text = state.get('scenario_text')
            
            # دریافت پیشرفت
            progress = await self.db.get_scenario_progress(user_id, scenario_text)
            
            if not progress:
                await event.edit(
                    "❌ پیشرفت قبلی پیدا نشد!",
                    buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                )
                return
            
            # تنظیم start_index برای ادامه
            state['start_index'] = progress['last_account_index']
            state['resume_mode'] = True
            
            active_accounts = state['active_accounts']
            remaining = len(active_accounts) - progress['last_account_index']
            
            await event.edit(
                f"▶️ **ادامه سناریو**\n\n"
                f"📊 از اکانت {progress['last_account_index'] + 1} ادامه می‌دهیم\n"
                f"📈 باقی‌مانده: {remaining} اکانت\n\n"
                f"چند تا اکانت برای ادامه استفاده شود؟\n\n"
                f"💡 **گزینه‌ها:**\n"
                f"• عدد بفرست (مثلاً `5`)\n"
                f"• `/all` - همه باقی‌مانده ({remaining} اکانت)\n"
                f"• `/from 80` - از اکانت 80 شروع کن\n"
                f"• `/from 80 to 100` - از 80 تا 100",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"restart_scenario"))
        async def restart_scenario_callback(event):
            """شروع سناریو از اول"""
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                await event.answer("❌ خطا! لطفاً دوباره تلاش کنید.", alert=True)
                return
            
            await event.answer()
            
            state = self.user_states[user_id]
            scenario_text = state.get('scenario_text')
            
            # حذف پیشرفت قبلی
            await self.db.delete_scenario_progress(user_id, scenario_text)
            
            # تنظیم برای شروع از اول
            state['start_index'] = 0
            state['resume_mode'] = False
            
            active_accounts = state['active_accounts']
            
            await event.edit(
                f"🔄 **شروع از اول**\n\n"
                f"📊 شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                f"چند تا اکانت برای اجرای سناریو استفاده شود؟\n\n"
                f"💡 **گزینه‌ها:**\n"
                f"• عدد بفرست (مثلاً `5`) - از اول شروع میشه\n"
                f"• `/all` - همه اکانت‌ها\n"
                f"• `/from 70` - از اکانت 70 شروع کن\n"
                f"• `/from 70 to 100` - از 70 تا 100",
                buttons=Button.inline("❌ لغو", b"cancel")
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"manual_select_scenario"))
        async def manual_select_scenario_callback(event):
            """انتخاب دستی محدوده اکانت‌ها"""
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                await event.answer("❌ خطا! لطفاً دوباره تلاش کنید.", alert=True)
                return
            
            await event.answer()
            
            state = self.user_states[user_id]
            
            # تنظیم برای انتخاب دستی
            state['start_index'] = 0
            state['resume_mode'] = False
            
            active_accounts = state['active_accounts']
            
            await event.edit(
                f"🎯 **انتخاب دستی محدوده**\n\n"
                f"📊 شما {len(active_accounts)} اکانت فعال دارید.\n\n"
                f"چند تا اکانت برای اجرای سناریو استفاده شود؟\n\n"
                f"💡 **گزینه‌ها:**\n"
                f"• عدد بفرست (مثلاً `5`) - از اول شروع میشه\n"
                f"• `/all` - همه اکانت‌ها\n"
                f"• `/from 70` - از اکانت 70 شروع کن\n"
                f"• `/from 70 to 100` - از 70 تا 100",
                buttons=Button.inline("❌ لغو", b"cancel")
            )

        @self.bot.on(events.NewMessage(func=lambda e: e.message.document and e.sender_id in Config.ADMIN_IDS))
        async def document_handler(event):
            """هندلر دریافت فایل (برای ریستور بکاپ)"""
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                return
            
            state = self.user_states[user_id]
            step = state.get('step')
            
            if step == 'restore_backup':
                # دریافت فایل بکاپ
                document = event.message.document
                
                # بررسی پسوند فایل
                if not document.attributes[0].file_name.endswith('.db'):
                    await event.respond(
                        "❌ فایل نامعتبر است! فقط فایل‌های .db پذیرفته می‌شوند.",
                        buttons=Button.inline("🔙 پنل ادمین", b"admin_panel")
                    )
                    return
                
                progress_msg = await event.respond("⏳ در حال دانلود فایل...")
                
                try:
                    # دانلود فایل
                    temp_backup_path = Path('data') / 'temp_restore.db'
                    await event.message.download_media(file=str(temp_backup_path))
                    
                    await progress_msg.edit("⏳ در حال ریستور دیتابیس...")
                    
                    # ریستور دیتابیس
                    result = await self.backup_manager.restore_database(
                        str(temp_backup_path),
                        Config.DATABASE_PATH
                    )
                    
                    if result['success']:
                        await progress_msg.edit(
                            "✅ **دیتابیس با موفقیت ریستور شد!**\n\n"
                            "⚠️ **توجه:** برای اعمال تغییرات، ربات را ریستارت کنید.\n\n"
                            "یک بکاپ امنیتی از دیتابیس قبلی ساخته شد.",
                            buttons=Button.inline("🔙 پنل ادمین", b"admin_panel")
                        )
                        
                        await self.db.log_action('restore_backup', user_id, 'success')
                    else:
                        await progress_msg.edit(
                            f"❌ **خطا در ریستور:**\n{result['message']}",
                            buttons=Button.inline("🔙 پنل ادمین", b"admin_panel")
                        )
                    
                    # حذف فایل موقت
                    if temp_backup_path.exists():
                        temp_backup_path.unlink()
                    
                    del self.user_states[user_id]
                    
                except Exception as e:
                    logger.exception(f"خطا در ریستور: {e}")
                    await progress_msg.edit(
                        f"❌ **خطا:**\n{str(e)}",
                        buttons=Button.inline("🔙 پنل ادمین", b"admin_panel")
                    )
                    del self.user_states[user_id]

        
        # ─── تابع کمکی برای ساخت صفحه یادداشت‌ها ───────────────────────────
        async def _build_notes_page(user_id: int, page: int = 0) -> tuple:
            """
            ساخت متن و دکمه‌های صفحه‌بندی یادداشت‌ها
            Returns: (text, buttons)
            """
            BOTS_PER_PAGE = 5  # تعداد ربات در هر صفحه

            notes = await self.note_manager.get_user_notes(user_id)

            if not notes:
                return (
                    "📝 **یادداشت‌های من**\n\n"
                    "❌ شما هنوز یادداشتی ثبت نکرده‌اید.\n\n"
                    "💡 بعد از اجرای سناریوها، می‌توانید یادداشت ثبت کنید.",
                    [[Button.inline("🔙 بازگشت", b"back_to_menu")]]
                )

            # گروه‌بندی بر اساس ربات
            bots_notes = {}
            for note in notes:
                bot = note['bot_username']
                if bot not in bots_notes:
                    bots_notes[bot] = []
                bots_notes[bot].append(note)

            bot_list = list(bots_notes.items())
            total_bots = len(bot_list)
            total_pages = max(1, (total_bots + BOTS_PER_PAGE - 1) // BOTS_PER_PAGE)
            page = max(0, min(page, total_pages - 1))

            start = page * BOTS_PER_PAGE
            end = start + BOTS_PER_PAGE
            page_bots = bot_list[start:end]

            text = "📝 **یادداشت‌های من**\n\n"
            text += f"📊 تعداد کل: {len(notes)} یادداشت\n"
            text += f"🤖 تعداد رباتها: {total_bots}\n"
            text += f"📄 صفحه {page + 1} از {total_pages}\n\n"

            for bot_username, bot_notes in page_bots:
                text += f"🤖 **@{bot_username}** ({len(bot_notes)} یادداشت)\n"
                for note in bot_notes[:2]:
                    note_preview = note['note_text'][:60]
                    if len(note['note_text']) > 60:
                        note_preview += "..."
                    text += f"   • {note_preview}\n"
                    text += f"     📅 {note['created_at'][:10]}\n"
                if len(bot_notes) > 2:
                    text += f"   ... و {len(bot_notes) - 2} یادداشت دیگر\n"
                text += "\n"

            text += "💡 برای مشاهده کامل: `/notes @bot_username`"

            # دکمه‌های ناوبری
            nav_row = []
            if page > 0:
                nav_row.append(Button.inline("◀️ قبلی", f"notes_page_{page - 1}".encode()))
            if page < total_pages - 1:
                nav_row.append(Button.inline("بعدی ▶️", f"notes_page_{page + 1}".encode()))

            buttons = []
            if nav_row:
                buttons.append(nav_row)
            buttons.append([
                Button.inline("📄 دریافت TXT", b"notes_export_txt"),
                Button.inline("🗑 حذف یادداشت", b"delete_note_menu"),
            ])
            buttons.append([Button.inline("📋 تاریخچه رباتها", b"bot_history")])
            buttons.append([Button.inline("🔙 بازگشت", b"back_to_menu")])

            return text, buttons

        @self.bot.on(events.CallbackQuery(pattern=b"my_notes"))
        async def my_notes_callback(event):
            """نمایش یادداشت‌های کاربر - صفحه اول"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id
            text, buttons = await _build_notes_page(user_id, page=0)
            await event.edit(text, buttons=buttons)

        @self.bot.on(events.CallbackQuery(pattern=rb"notes_page_(\d+)"))
        async def notes_page_callback(event):
            """صفحه‌بندی یادداشت‌ها"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id
            page = int(event.pattern_match.group(1))
            text, buttons = await _build_notes_page(user_id, page=page)
            await event.edit(text, buttons=buttons)

        @self.bot.on(events.CallbackQuery(pattern=b"notes_export_txt"))
        async def notes_export_txt_callback(event):
            """ارسال همه یادداشت‌ها به صورت فایل TXT"""
            if not await self._check_admin_access(event):
                return
            await event.answer("⏳ در حال آماده‌سازی فایل...", alert=False)

            user_id = event.sender_id
            notes = await self.note_manager.get_user_notes(user_id)

            if not notes:
                await event.answer("❌ یادداشتی وجود ندارد!", alert=True)
                return

            # ساخت محتوای فایل
            lines = []
            lines.append("=" * 50)
            lines.append("📝 یادداشت‌های من")
            lines.append(f"تاریخ خروجی: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            lines.append(f"تعداد کل: {len(notes)} یادداشت")
            lines.append("=" * 50)
            lines.append("")

            # گروه‌بندی بر اساس ربات
            bots_notes = {}
            for note in notes:
                bot = note['bot_username']
                if bot not in bots_notes:
                    bots_notes[bot] = []
                bots_notes[bot].append(note)

            for bot_username, bot_notes in bots_notes.items():
                lines.append(f"🤖 ربات: @{bot_username}  ({len(bot_notes)} یادداشت)")
                lines.append("-" * 40)
                for i, note in enumerate(bot_notes, 1):
                    lines.append(f"  [{i}] ID: {note['id']}")
                    lines.append(f"  📅 ایجاد: {note['created_at'][:16]}")
                    if note['updated_at'] != note['created_at']:
                        lines.append(f"  ✏️ ویرایش: {note['updated_at'][:16]}")
                    lines.append(f"  📝 متن:")
                    for ln in note['note_text'].splitlines():
                        lines.append(f"      {ln}")
                    lines.append("")
                lines.append("")

            content = "\n".join(lines)

            import io
            file_bytes = io.BytesIO(content.encode("utf-8"))
            file_bytes.name = f"notes_{user_id}.txt"

            await self.bot.send_file(
                event.chat_id,
                file_bytes,
                caption=f"📄 **یادداشت‌های شما**\n📊 {len(notes)} یادداشت از {len(bots_notes)} ربات",
                attributes=[],
            )

        # ─── تابع کمکی برای ساخت صفحه تاریخچه ──────────────────────────────
        async def _build_history_page(user_id: int, page: int = 0) -> tuple:
            """
            ساخت متن و دکمه‌های صفحه‌بندی تاریخچه رباتها
            گروه‌بندی بر اساس تاریخ (امروز / دیروز / این هفته / قدیمی‌تر)
            Returns: (text, buttons)
            """
            from datetime import date, timedelta

            ITEMS_PER_PAGE = 8  # تعداد ربات در هر صفحه

            history = await self.db.get_bot_history(user_id, limit=500)

            if not history:
                return (
                    "📋 **تاریخچه رباتها**\n\n"
                    "❌ هنوز هیچ سناریویی اجرا نشده.\n\n"
                    "💡 بعد از اجرای سناریو، رباتها اینجا ثبت می‌شوند.",
                    [[Button.inline("🔙 بازگشت", b"my_notes")]]
                )

            today = date.today()
            yesterday = today - timedelta(days=1)
            week_ago = today - timedelta(days=7)

            # گروه‌بندی بر اساس تاریخ
            groups = {"امروز 🌅": [], "دیروز 🌙": [], "این هفته 📅": [], "قدیمی‌تر 🗂": []}
            for row in history:
                try:
                    row_date = date.fromisoformat(row['executed_at'][:10])
                except Exception:
                    row_date = today
                if row_date == today:
                    groups["امروز 🌅"].append(row)
                elif row_date == yesterday:
                    groups["دیروز 🌙"].append(row)
                elif row_date >= week_ago:
                    groups["این هفته 📅"].append(row)
                else:
                    groups["قدیمی‌تر 🗂"].append(row)

            # فهرست مسطح برای صفحه‌بندی (هر آیتم یک اجرا)
            flat = []
            for group_name, rows in groups.items():
                if rows:
                    flat.append(('header', group_name, len(rows)))
                    for row in rows:
                        flat.append(('row', row))

            # صفحه‌بندی روی ردیف‌های واقعی (نه هدرها)
            real_rows = [i for i, item in enumerate(flat) if item[0] == 'row']
            total_items = len(real_rows)
            total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
            page = max(0, min(page, total_pages - 1))

            page_start_real = page * ITEMS_PER_PAGE
            page_end_real = page_start_real + ITEMS_PER_PAGE
            visible_real_indices = set(real_rows[page_start_real:page_end_real])

            # پیدا کردن هدرهایی که باید نشون داده بشن
            visible_header_indices = set()
            for idx in visible_real_indices:
                # هدر قبل از این ردیف
                for h in range(idx - 1, -1, -1):
                    if flat[h][0] == 'header':
                        visible_header_indices.add(h)
                        break

            text = "📋 **تاریخچه رباتها**\n"
            text += f"📊 {total_items} اجرا ثبت شده\n"
            text += f"📄 صفحه {page + 1} از {total_pages}\n"
            text += "─" * 30 + "\n\n"

            last_header = None
            for idx, item in enumerate(flat):
                if item[0] == 'header':
                    if idx in visible_header_indices:
                        last_header = item[1]
                        text += f"\n**{item[1]}** ({item[2]} اجرا)\n"
                        text += "━" * 20 + "\n"
                elif item[0] == 'row' and idx in visible_real_indices:
                    row = item[1]
                    bot = row['bot_username']
                    t = row['executed_at'][11:16]  # HH:MM
                    d = row['executed_at'][:10]
                    total_acc = row['accounts_total']
                    ok = row['accounts_success']
                    fail = row['accounts_failed']
                    rate = int((ok / total_acc) * 100) if total_acc > 0 else 0
                    # آیکون موفقیت
                    if rate >= 90:
                        icon = "🟢"
                    elif rate >= 60:
                        icon = "🟡"
                    else:
                        icon = "🔴"
                    text += f"{icon} **@{bot}**\n"
                    text += f"   🕐 {d} {t}  |  👥 {total_acc} اکانت\n"
                    text += f"   ✅ {ok} موفق  ❌ {fail} ناموفق  ({rate}%)\n\n"

            text += "─" * 30 + "\n"
            text += "💡 `/history @bot` برای جزئیات یک ربات"

            # دکمه‌های ناوبری
            nav_row = []
            if page > 0:
                nav_row.append(Button.inline("◀️ قبلی", f"history_page_{page - 1}".encode()))
            if page < total_pages - 1:
                nav_row.append(Button.inline("بعدی ▶️", f"history_page_{page + 1}".encode()))

            buttons = []
            if nav_row:
                buttons.append(nav_row)
            buttons.append([
                Button.inline("📄 دریافت TXT", b"history_export_txt"),
            ])
            buttons.append([Button.inline("🔙 یادداشت‌ها", b"my_notes")])

            return text, buttons

        @self.bot.on(events.CallbackQuery(pattern=b"bot_history"))
        async def bot_history_callback(event):
            """نمایش تاریخچه رباتها - صفحه اول"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id
            text, buttons = await _build_history_page(user_id, page=0)
            await event.edit(text, buttons=buttons)

        @self.bot.on(events.CallbackQuery(pattern=rb"history_page_(\d+)"))
        async def history_page_callback(event):
            """صفحه‌بندی تاریخچه"""
            if not await self._check_admin_access(event):
                return
            await event.answer()
            user_id = event.sender_id
            page = int(event.pattern_match.group(1))
            text, buttons = await _build_history_page(user_id, page=page)
            await event.edit(text, buttons=buttons)

        @self.bot.on(events.CallbackQuery(pattern=b"history_export_txt"))
        async def history_export_txt_callback(event):
            """ارسال تاریخچه به صورت فایل TXT"""
            if not await self._check_admin_access(event):
                return
            await event.answer("⏳ در حال آماده‌سازی فایل...", alert=False)

            user_id = event.sender_id
            history = await self.db.get_bot_history(user_id, limit=500)

            if not history:
                await event.answer("❌ تاریخچه‌ای وجود ندارد!", alert=True)
                return

            lines = []
            lines.append("=" * 50)
            lines.append("📋 تاریخچه اجرای سناریو روی رباتها")
            lines.append(f"تاریخ خروجی: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            lines.append(f"تعداد کل اجراها: {len(history)}")
            lines.append("=" * 50)
            lines.append("")

            current_date = None
            for row in history:
                row_date = row['executed_at'][:10]
                if row_date != current_date:
                    current_date = row_date
                    lines.append(f"\n📅 {row_date}")
                    lines.append("-" * 40)
                t = row['executed_at'][11:16]
                total_acc = row['accounts_total']
                ok = row['accounts_success']
                fail = row['accounts_failed']
                rate = int((ok / total_acc) * 100) if total_acc > 0 else 0
                lines.append(f"  {t}  @{row['bot_username']}")
                lines.append(f"       👥 {total_acc} اکانت  |  ✅ {ok}  ❌ {fail}  ({rate}%)")
                lines.append("")

            content = "\n".join(lines)
            import io
            file_bytes = io.BytesIO(content.encode("utf-8"))
            file_bytes.name = f"history_{user_id}.txt"

            await self.bot.send_file(
                event.chat_id,
                file_bytes,
                caption=f"📋 **تاریخچه رباتها**\n📊 {len(history)} اجرا ثبت شده",
                attributes=[],
            )

        @self.bot.on(events.NewMessage(pattern='/history'))
        async def history_command_handler(event):
            """نمایش تاریخچه اجراها برای یک ربات خاص"""
            user_id = event.sender_id
            is_creator = user_id in Config.ADMIN_IDS
            is_admin = await self.db.is_admin(user_id)
            if not is_creator and not is_admin:
                await event.respond("⛔️ این قابلیت فقط برای ادمین‌ها در دسترس است!")
                return
            try:
                parts = event.message.text.split()
                if len(parts) < 2:
                    await event.respond(
                        "❌ فرمت نادرست!\n\n"
                        "استفاده: `/history @bot_username`\n"
                        "مثال: `/history @MyBot`"
                    )
                    return
                bot_username = parts[1].lstrip('@')
                rows = await self.db.get_bot_history_by_username(user_id, bot_username)
                if not rows:
                    await event.respond(
                        f"📋 **تاریخچه @{bot_username}**\n\n"
                        f"❌ هیچ اجرایی برای این ربات ثبت نشده."
                    )
                    return
                text = f"📋 **تاریخچه @{bot_username}**\n"
                text += f"📊 {len(rows)} بار اجرا شده\n"
                text += "─" * 30 + "\n\n"
                total_ok = sum(r['accounts_success'] for r in rows)
                total_fail = sum(r['accounts_failed'] for r in rows)
                total_all = sum(r['accounts_total'] for r in rows)
                overall_rate = int((total_ok / total_all) * 100) if total_all > 0 else 0
                text += f"📈 **آمار کلی:**\n"
                text += f"   👥 {total_all} اکانت  |  ✅ {total_ok}  ❌ {total_fail}  ({overall_rate}%)\n\n"
                text += "**جزئیات اجراها:**\n"
                for i, row in enumerate(rows[:20], 1):
                    d = row['executed_at'][:10]
                    t = row['executed_at'][11:16]
                    ok = row['accounts_success']
                    fail = row['accounts_failed']
                    total_acc = row['accounts_total']
                    rate = int((ok / total_acc) * 100) if total_acc > 0 else 0
                    icon = "🟢" if rate >= 90 else ("🟡" if rate >= 60 else "🔴")
                    text += f"{icon} **#{i}** — {d} {t}\n"
                    text += f"   👥 {total_acc}  ✅ {ok}  ❌ {fail}  ({rate}%)\n\n"
                if len(rows) > 20:
                    text += f"... و {len(rows) - 20} اجرای دیگر\n"
                await event.respond(text)
            except Exception as e:
                logger.exception(f"خطا در نمایش تاریخچه: {e}")
                await event.respond(f"❌ خطا: {str(e)}")

        @self.bot.on(events.CallbackQuery(pattern=b"note_yes"))
        async def note_yes_callback(event):
            """کاربر می‌خواهد یادداشت بزند"""
            await event.answer()
            
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                await event.edit(
                    "❌ خطا! لطفاً دوباره تلاش کنید.",
                    buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                )
                return
            
            state = self.user_states[user_id]
            step = state.get('step')
            
            if step == 'ask_note_single':
                bot_username = state['bot_username']
                state['step'] = 'waiting_note_single'
                
                await event.edit(
                    f"📝 **یادداشت برای @{bot_username}**\n\n"
                    f"لطفاً یادداشت خود را ارسال کنید:\n\n"
                    f"💡 می‌توانید اطلاعاتی مثل:\n"
                    f"• لینک رفرال\n"
                    f"• نکات مهم\n"
                    f"• تنظیمات خاص\n"
                    f"• نتایج قبلی\n\n"
                    f"را ثبت کنید.",
                    buttons=Button.inline("❌ لغو", b"note_no")
                )
            
            elif step == 'ask_note_multi':
                bots_scenarios = state['bots_scenarios']
                current_index = state['current_bot_index']
                bot_username = bots_scenarios[current_index]['bot_username']
                state['step'] = 'waiting_note_multi'
                
                await event.edit(
                    f"📝 **یادداشت برای @{bot_username}**\n\n"
                    f"لطفاً یادداشت خود را ارسال کنید:\n\n"
                    f"💡 می‌توانید اطلاعاتی مثل:\n"
                    f"• لینک رفرال\n"
                    f"• نکات مهم\n"
                    f"• تنظیمات خاص\n"
                    f"• نتایج قبلی\n\n"
                    f"را ثبت کنید.",
                    buttons=Button.inline("❌ لغو", b"note_skip")
                )
            
        @self.bot.on(events.CallbackQuery(pattern=b"note_no"))
        async def note_no_callback(event):
            """کاربر نمی‌خواهد یادداشت بزند (تک ربات)"""
            await event.answer()
            
            user_id = event.sender_id
            
            if user_id in self.user_states:
                del self.user_states[user_id]
            
            await event.edit(
                "✅ **تمام شد!**\n\n"
                "سناریو با موفقیت اجرا شد.",
                buttons=[
                    [Button.inline("🎯 سناریو جدید", b"advanced_scenario")],
                    [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                ]
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"note_skip"))
        async def note_skip_callback(event):
            """رد کردن یادداشت فعلی و رفتن به بعدی (چند ربات)"""
            await event.answer()
            
            user_id = event.sender_id
            
            if user_id not in self.user_states:
                await event.edit(
                    "❌ خطا! لطفاً دوباره تلاش کنید.",
                    buttons=Button.inline("🔙 منوی اصلی", b"back_to_menu")
                )
                return
            
            state = self.user_states[user_id]
            bots_scenarios = state.get('bots_scenarios', [])
            current_index = state.get('current_bot_index', 0)
            
            # رفتن به ربات بعدی
            next_index = current_index + 1
            
            if next_index < len(bots_scenarios):
                # ربات بعدی وجود دارد
                state['current_bot_index'] = next_index
                state['step'] = 'ask_note_multi'
                next_bot = bots_scenarios[next_index]['bot_username']
                
                await event.edit(
                    f"📝 **یادداشت برای ربات @{next_bot}**\n\n"
                    f"آیا می‌خواهید یادداشتی برای این ربات ثبت کنید؟\n\n"
                    f"💡 یادداشت‌ها به شما کمک می‌کنند تا اطلاعات مهم درباره هر ربات را ذخیره کنید.",
                    buttons=[
                        [Button.inline("✅ بله، یادداشت می‌زنم", b"note_yes")],
                        [Button.inline("⏭ بعدی", b"note_skip")],
                        [Button.inline("❌ نه، برای هیچکدام", b"note_no_all")]
                    ]
                )
            else:
                # تمام رباتها تمام شدند
                del self.user_states[user_id]
                
                await event.edit(
                    "✅ **تمام شد!**\n\n"
                    "سناریو با موفقیت اجرا شد.",
                    buttons=[
                        [Button.inline("🎯 سناریو جدید", b"advanced_scenario")],
                        [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                    ]
                )
        
        @self.bot.on(events.CallbackQuery(pattern=b"note_no_all"))
        async def note_no_all_callback(event):
            """کاربر نمی‌خواهد برای هیچ رباتی یادداشت بزند"""
            await event.answer()
            
            user_id = event.sender_id
            
            if user_id in self.user_states:
                del self.user_states[user_id]
            
            await event.edit(
                "✅ **تمام شد!**\n\n"
                "سناریو با موفقیت اجرا شد.",
                buttons=[
                    [Button.inline("🎯 سناریو جدید", b"advanced_scenario")],
                    [Button.inline("🔙 منوی اصلی", b"back_to_menu")]
                ]
            )
        
        @self.bot.on(events.CallbackQuery(pattern=b"delete_note_menu"))
        async def delete_note_menu_callback(event):
            """منوی حذف یادداشت"""
            # بررسی دسترسی ادمین
            if not await self._check_admin_access(event):
                return
            
            await event.answer()
            
            await event.edit(
                "🗑 **حذف یادداشت**\n\n"
                "برای حذف یادداشت، از دستور زیر استفاده کنید:\n\n"
                "`/deletenote NOTE_ID`\n\n"
                "💡 برای مشاهده ID یادداشت‌ها:\n"
                "`/notes @bot_username`",
                buttons=Button.inline("🔙 بازگشت", b"my_notes")
            )
        
        @self.bot.on(events.NewMessage(pattern='/notes'))
        async def notes_command_handler(event):
            """نمایش یادداشت‌های یک ربات خاص"""
            # بررسی دسترسی ادمین
            user_id = event.sender_id
            is_creator = user_id in Config.ADMIN_IDS
            is_admin = await self.db.is_admin(user_id)
            
            if not is_creator and not is_admin:
                await event.respond("⛔️ این قابلیت فقط برای ادمین‌ها در دسترس است!")
                return
            
            try:
                # دریافت یوزرنیم ربات
                parts = event.message.text.split()
                if len(parts) < 2:
                    await event.respond(
                        "❌ فرمت نادرست!\n\n"
                        "استفاده: `/notes @bot_username`\n"
                        "مثال: `/notes @MyBot`"
                    )
                    return
                
                bot_username = parts[1].lstrip('@')
                
                # دریافت یادداشت‌های ربات
                notes = await self.note_manager.get_bot_notes(user_id, bot_username)
                
                if not notes:
                    await event.respond(
                        f"📝 **یادداشت‌های @{bot_username}**\n\n"
                        f"❌ شما برای این ربات یادداشتی ثبت نکرده‌اید."
                    )
                    return
                
                text = f"📝 **یادداشت‌های @{bot_username}**\n\n"
                text += f"📊 تعداد: {len(notes)} یادداشت\n\n"
                
                for i, note in enumerate(notes, 1):
                    text += f"{'=' * 40}\n"
                    text += f"📌 **یادداشت #{i}** (ID: `{note['id']}`)\n"
                    text += f"📅 تاریخ: {note['created_at'][:16]}\n"
                    if note['updated_at'] != note['created_at']:
                        text += f"✏️ ویرایش: {note['updated_at'][:16]}\n"
                    text += f"\n{note['note_text']}\n\n"
                
                text += f"{'=' * 40}\n\n"
                text += "💡 **دستورات:**\n"
                text += "• حذف: `/deletenote NOTE_ID`\n"
                text += "• ویرایش: `/editnote NOTE_ID`"
                
                await event.respond(text)
                
            except Exception as e:
                logger.exception(f"خطا در نمایش یادداشت‌ها: {e}")
                await event.respond(f"❌ خطا: {str(e)}")
        
        @self.bot.on(events.NewMessage(pattern='/deletenote'))
        async def delete_note_command_handler(event):
            """حذف یادداشت"""
            # بررسی دسترسی ادمین
            user_id = event.sender_id
            is_creator = user_id in Config.ADMIN_IDS
            is_admin = await self.db.is_admin(user_id)
            
            if not is_creator and not is_admin:
                await event.respond("⛔️ این قابلیت فقط برای ادمین‌ها در دسترس است!")
                return
            
            try:
                # دریافت ID یادداشت
                parts = event.message.text.split()
                if len(parts) < 2:
                    await event.respond(
                        "❌ فرمت نادرست!\n\n"
                        "استفاده: `/deletenote NOTE_ID`\n"
                        "مثال: `/deletenote 5`"
                    )
                    return
                
                note_id = int(parts[1])
                
                # حذف یادداشت
                success = await self.note_manager.delete_note(note_id, user_id)
                
                if success:
                    await event.respond(
                        f"✅ **یادداشت حذف شد!**\n\n"
                        f"🗑 یادداشت با ID `{note_id}` حذف شد."
                    )
                    await self.db.log_action('delete_note', user_id, str(note_id))
                else:
                    await event.respond("❌ خطا در حذف یادداشت! شاید این یادداشت متعلق به شما نباشد.")
                    
            except ValueError:
                await event.respond("❌ ID نامعتبر است! لطفاً یک عدد صحیح وارد کنید.")
            except Exception as e:
                await event.respond(f"❌ خطا: {str(e)}")
        
        @self.bot.on(events.NewMessage(pattern='/editnote'))
        async def edit_note_command_handler(event):
            """ویرایش یادداشت"""
            # بررسی دسترسی ادمین
            user_id = event.sender_id
            is_creator = user_id in Config.ADMIN_IDS
            is_admin = await self.db.is_admin(user_id)
            
            if not is_creator and not is_admin:
                await event.respond("⛔️ این قابلیت فقط برای ادمین‌ها در دسترس است!")
                return
            
            try:
                # دریافت ID یادداشت
                parts = event.message.text.split(maxsplit=1)
                if len(parts) < 2:
                    await event.respond(
                        "❌ فرمت نادرست!\n\n"
                        "استفاده: `/editnote NOTE_ID`\n"
                        "مثال: `/editnote 5`\n\n"
                        "سپس متن جدید را ارسال کنید."
                    )
                    return
                
                note_id = int(parts[1])
                
                # ذخیره state برای دریافت متن جدید
                self.user_states[user_id] = {
                    'step': 'edit_note',
                    'note_id': note_id
                }
                
                await event.respond(
                    f"✏️ **ویرایش یادداشت #{note_id}**\n\n"
                    f"لطفاً متن جدید یادداشت را ارسال کنید:",
                    buttons=Button.inline("❌ لغو", b"cancel")
                )
                    
            except ValueError:
                await event.respond("❌ ID نامعتبر است! لطفاً یک عدد صحیح وارد کنید.")
            except Exception as e:
                await event.respond(f"❌ خطا: {str(e)}")
