"""ماژول اتوماسیون پیشرفته ربات‌ها"""
import logging
import asyncio
import random
import string
import re
from typing import Optional, Dict, List
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from pathlib import Path

from src.config import Config

logger = logging.getLogger(__name__)

class BotAutomation:
    """کلاس اتوماسیون پیشرفته ربات‌ها"""
    
    def __init__(self, api_id: Optional[int] = None, api_hash: Optional[str] = None):
        """مقداردهی اولیه"""
        self.api_id = api_id or Config.API_ID
        self.api_hash = api_hash or Config.API_HASH
    
    @staticmethod
    def _generate_random_string(length: int) -> str:
        """
        تولید رشته تصادفی
        
        Args:
            length: طول رشته
            
        Returns:
            رشته تصادفی
        """
        # استفاده از حروف کوچک و اعداد
        characters = string.ascii_lowercase + string.digits
        return ''.join(random.choice(characters) for _ in range(length))
    
    @staticmethod
    def _replace_variables(text: str) -> str:
        """
        جایگزینی متغیرهای دینامیک در متن
        
        متغیرهای پشتیبانی شده:
        - {random:N} → رشته تصادفی N حرفی (حروف کوچک + اعداد)
        - {random_upper:N} → رشته تصادفی N حرفی (حروف بزرگ + اعداد)
        - {random_num:N} → عدد تصادفی N رقمی
        
        Args:
            text: متن ورودی
            
        Returns:
            متن با متغیرهای جایگزین شده
        """
        # جایگزینی {random:N}
        pattern = r'\{random:(\d+)\}'
        matches = re.findall(pattern, text)
        for match in matches:
            length = int(match)
            random_str = BotAutomation._generate_random_string(length)
            text = text.replace(f'{{random:{match}}}', random_str, 1)
        
        # جایگزینی {random_upper:N}
        pattern = r'\{random_upper:(\d+)\}'
        matches = re.findall(pattern, text)
        for match in matches:
            length = int(match)
            characters = string.ascii_uppercase + string.digits
            random_str = ''.join(random.choice(characters) for _ in range(length))
            text = text.replace(f'{{random_upper:{match}}}', random_str, 1)
        
        # جایگزینی {random_num:N}
        pattern = r'\{random_num:(\d+)\}'
        matches = re.findall(pattern, text)
        for match in matches:
            length = int(match)
            random_num = ''.join(random.choice(string.digits) for _ in range(length))
            text = text.replace(f'{{random_num:{match}}}', random_num, 1)
        
        return text

    @staticmethod
    @staticmethod
    def resolve_parent_ref(scenario_text: str, idx: int, selected_accounts: list, extracted_codes: dict, successful_sessions: list) -> Optional[str]:
        """
        محاسبه رفرال والد بر اساس موقعیت در زنجیره یا درخت (فقط با شمارش اکانت‌های موفق)
        """
        pattern = r'\{(parent_ref(?:_id)?)(?::(chain|tree):(\d+))?(?:\|([a-zA-Z0-9_-]+))?\}'
        match = re.search(pattern, scenario_text)
        if not match:
            return None
        
        ref_type = match.group(2) or 'chain'
        ref_val = int(match.group(3)) if match.group(3) else 3
        root_ref = match.group(4) or ''
        
        if idx == 0 or not successful_sessions:
            return root_ref
        
        if ref_type == 'chain':
            K = ref_val
            pos = idx % K
            if pos == 0:
                return root_ref
            else:
                parent_session = successful_sessions[idx - 1]
                return extracted_codes.get(parent_session, root_ref)
                
        elif ref_type == 'tree':
            M = ref_val
            parent_idx = (idx - 1) // M
            if parent_idx < len(successful_sessions):
                parent_session = successful_sessions[parent_idx]
                return extracted_codes.get(parent_session, root_ref)
            else:
                return root_ref
                
        return root_ref
    
    async def execute_scenario(self, session_path: str, bot_username: str, 
                               scenario: List[Dict], db=None, parent_ref: Optional[str] = None) -> Dict[str, any]:
        """
        اجرای سناریو کامل
        
        Args:
            session_path: مسیر فایل سشن
            bot_username: یوزرنیم ربات
            scenario: لیست مراحل سناریو
            db: دیتابیس برای غیرفعال کردن سشن نامعتبر (اختیاری)
            
        Returns:
            دیکشنری حاوی وضعیت و پیام
        """
        client = None
        
        try:
            # بارگذاری سشن
            session_string = Path(session_path).read_text(encoding='utf-8')
            
            client = TelegramClient(
                StringSession(session_string),
                self.api_id,
                self.api_hash
            )
            
            await client.connect()
            
            if not await client.is_user_authorized():
                # سشن نامعتبر — غیرفعال کن و فایل رو منتقل کن
                logger.warning(f"سشن نامعتبر: {session_path}")
                if db:
                    await db.invalidate_session(session_path)
                return {
                    'success': False,
                    'message': 'سشن نامعتبر است',
                    'invalid_session': True
                }
            
            # حذف @ از یوزرنیم — یا لینک خصوصی t.me/+hash
            bot_username = bot_username.strip()
            if bot_username.startswith('@'):
                bot_username = bot_username[1:]

            # دریافت entity ربات/گروه
            try:
                # لینک خصوصی: https://t.me/+HASH یا https://t.me/joinchat/HASH
                if 't.me/+' in bot_username or 't.me/joinchat/' in bot_username or \
                   (bot_username.startswith('+') and len(bot_username) > 5):
                    from telethon.tl.functions.messages import CheckChatInviteRequest
                    if 't.me/+' in bot_username:
                        hash_part = bot_username.split('t.me/+')[-1].strip('/')
                    elif 't.me/joinchat/' in bot_username:
                        hash_part = bot_username.split('t.me/joinchat/')[-1].strip('/')
                    else:
                        hash_part = bot_username.lstrip('+')
                    invite = await client(CheckChatInviteRequest(hash_part))
                    if hasattr(invite, 'chat'):
                        bot = invite.chat
                    elif hasattr(invite, 'chats') and invite.chats:
                        bot = invite.chats[0]
                    else:
                        raise Exception("گروه از لینک خصوصی پیدا نشد")
                else:
                    bot = await client.get_entity(bot_username)
            except Exception as e:
                logger.error(f"خطا در پیدا کردن ربات/گروه: {e}")
                return {
                    'success': False,
                    'message': f'ربات/گروه پیدا نشد: {str(e)[:50]}'
                }
            
            logger.info(f"شروع اجرای سناریو برای @{bot_username}")
            
            executed_steps = []
            auto_leave_channels = []  # کانال‌هایی که باید در پایان لفت داده شوند
            smart_delay_enabled = False  # حالت صبر هوشمند
            smart_delay_timeout = 20    # timeout پیش‌فرض برای smart_delay
            
            is_new_user = True
            extracted_ref_code = None
            # اجرای هر مرحله
            for step_num, step in enumerate(scenario, 1):
                action = step.get('action')
                value = step.get('value', '')
                delay = step.get('delay', 2)
                
                # جایگزینی متغیرهای دینامیک
                value = self._replace_variables(value)
                if parent_ref:
                    value = value.replace('{parent_ref}', parent_ref).replace('{parent_ref_id}', parent_ref)
                
                logger.info(f"مرحله {step_num}: {action} - {value}")
                
                try:
                    if action == 'start':
                        # بررسی اینکه آیا اکانت قبلاً ربات را استارت زده است
                        try:
                            history = await client.get_messages(bot, limit=1)
                            if history and len(history) > 0:
                                is_new_user = False
                                logger.info(f"اکانت {session_path} قبلاً ربات @{bot_username} را استارت زده است.")
                                executed_steps.append("ℹ️ این اکانت قبلاً ربات را استارت زده است (به عنوان رفرال جدید والد شمرده نمی‌شود)")
                        except Exception as e:
                            logger.warning(f"خطا در بررسی تاریخچه ربات: {e}")
                        
                        # ارسال /start با پارامتر
                        await client.send_message(bot, f'/start {value}')
                        executed_steps.append(f"✅ استارت با رفرال: {value}")
                    
                    elif action == 'send':
                        # ارسال متن
                        await client.send_message(bot, value)
                        executed_steps.append(f"✅ ارسال پیام: {value[:30]}...")
                    
                    elif action == 'click':
                        # کلیک روی دکمه
                        messages = await client.get_messages(bot, limit=1)
                        
                        if messages and messages[0].buttons:
                            button_found = False
                            
                            # بررسی اینکه آیا value یک شماره است (با # یا بدون #)
                            button_index = None
                            if value.startswith('#'):
                                # فرمت: #0, #1, #2
                                try:
                                    button_index = int(value[1:])
                                except ValueError:
                                    pass
                            elif value.isdigit():
                                # فرمت: 0, 1, 2
                                button_index = int(value)
                            
                            if button_index is not None:
                                # کلیک با شماره دکمه
                                all_buttons = []
                                for row in messages[0].buttons:
                                    for button in row:
                                        all_buttons.append(button)
                                
                                if 0 <= button_index < len(all_buttons):
                                    button = all_buttons[button_index]
                                    button_text = button.text if hasattr(button, 'text') else str(button)
                                    await button.click()
                                    button_found = True
                                    executed_steps.append(f"✅ کلیک دکمه #{button_index}: {button_text}")
                                else:
                                    executed_steps.append(f"⚠️ دکمه شماره {button_index} وجود ندارد (تعداد: {len(all_buttons)})")
                            else:
                                # کلیک با جستجوی متن (روش قبلی)
                                for row in messages[0].buttons:
                                    for button in row:
                                        button_text = button.text if hasattr(button, 'text') else str(button)
                                        
                                        # جستجوی جزئی
                                        clean_button = ''.join(c for c in button_text if c.isalnum() or c.isspace()).strip().lower()
                                        clean_search = ''.join(c for c in value if c.isalnum() or c.isspace()).strip().lower()
                                        
                                        if clean_search in clean_button:
                                            await button.click()
                                            button_found = True
                                            executed_steps.append(f"✅ کلیک دکمه: {button_text}")
                                            break
                                    
                                    if button_found:
                                        break
                            
                            if not button_found and button_index is None:
                                executed_steps.append(f"⚠️ دکمه '{value}' پیدا نشد")
                        else:
                            executed_steps.append(f"⚠️ دکمه‌ای وجود ندارد")
                    
                    elif action == 'join':
                        # جوین کانال/گروه
                        channel_link = value.strip()
                        try:
                            if 'joinchat/' in channel_link or '/+' in channel_link:
                                hash_part = channel_link.split('/')[-1].replace('+', '')
                                await client(functions.messages.ImportChatInviteRequest(hash_part))
                            else:
                                username = channel_link.split('/')[-1].lstrip('@')
                                await client(functions.channels.JoinChannelRequest(username))
                            executed_steps.append(f"✅ جوین: {channel_link[:30]}")
                        except Exception as e:
                            executed_steps.append(f"❌ جوین ناموفق: {str(e)[:30]}")

                    elif action in ('join_leave', 'jl'):
                        # جوین + ثبت برای لفت خودکار در پایان سناریو
                        # فرمت: join_leave: https://t.me/channel
                        channel_link = value.strip()
                        try:
                            if 'joinchat/' in channel_link or '/+' in channel_link:
                                hash_part = channel_link.split('/')[-1].replace('+', '')
                                await client(functions.messages.ImportChatInviteRequest(hash_part))
                            else:
                                username = channel_link.split('/')[-1].lstrip('@')
                                await client(functions.channels.JoinChannelRequest(username))
                            # ثبت برای لفت خودکار در پایان
                            auto_leave_channels.append(channel_link)
                            executed_steps.append(f"✅ جوین (لفت خودکار در پایان): {channel_link[:30]}")
                        except Exception as e:
                            executed_steps.append(f"❌ جوین ناموفق: {str(e)[:30]}")
                    
                    elif action == 'leave':
                        # لفت کانال/گروه
                        channel_link = value.strip()
                        try:
                            # تجزیه لینک
                            username = channel_link.split('/')[-1].lstrip('@')
                            channel = await client.get_entity(username)
                            await client(functions.channels.LeaveChannelRequest(channel))
                            
                            executed_steps.append(f"✅ لفت: {channel_link[:30]}")
                        except Exception as e:
                            executed_steps.append(f"❌ لفت ناموفق: {str(e)[:30]}")
                    
                    elif action in ('auto_join', 'auto_join_leave', 'ajl'):
                        # جوین خودکار به تمام کانال‌های موجود در پیام ربات
                        # auto_join        ← جوین + کلیک آخرین دکمه
                        # auto_join_leave / ajl ← مثل auto_join ولی در پایان سناریو لفت می‌ده
                        # فرمت‌ها:
                        #   auto_join                  ← جوین همه + کلیک آخرین دکمه
                        #   auto_join: no_click        ← فقط جوین، بدون کلیک
                        #   auto_join: click=عضو شدم  ← جوین + کلیک دکمه خاص
                        is_auto_leave = action in ('auto_join_leave', 'ajl')
                        
                        try:
                            # تجزیه value
                            mode = 'last_button'  # پیش‌فرض
                            click_text = None
                            
                            if value:
                                v = value.strip().lower()
                                if v == 'no_click':
                                    mode = 'no_click'
                                elif v.startswith('click='):
                                    mode = 'custom_click'
                                    click_text = value.strip()[6:]
                                else:
                                    mode = 'last_button'
                            
                            # صبر کوتاه برای دریافت پیام ربات
                            await asyncio.sleep(1.5)
                            
                            # دریافت آخرین پیام ربات
                            messages = await client.get_messages(bot, limit=3)
                            
                            if not messages:
                                executed_steps.append(f"⚠️ {action}: پیامی از ربات دریافت نشد")
                                continue
                            
                            # استخراج لینک‌های t.me فقط از دکمه‌های شیشه‌ای (inline URL buttons)
                            found_links = []
                            all_buttons = []
                            
                            def normalize_tme_link(url: str) -> str:
                                """
                                normalize کردن لینک‌های تلگرام به فرمت استاندارد
                                حالت‌های ممکن:
                                  t.me/channel          → https://t.me/channel
                                  t.me/+HASH            → https://t.me/+HASH
                                  t.me/joinchat/HASH    → https://t.me/joinchat/HASH
                                  https://t.me/channel  → https://t.me/channel  (بدون تغییر)
                                  @channel              → https://t.me/channel
                                """
                                url = url.strip()
                                if not url:
                                    return ''
                                # حالت @username
                                if url.startswith('@'):
                                    return f"https://t.me/{url[1:]}"
                                # حالت بدون پروتکل
                                if url.startswith('t.me/'):
                                    return f"https://{url}"
                                # حالت //t.me/
                                if url.startswith('//t.me/'):
                                    return f"https:{url}"
                                # حالت http:// → تبدیل به https://
                                if url.startswith('http://t.me/'):
                                    return url.replace('http://', 'https://', 1)
                                # حالت کامل https://
                                return url
                            
                            for msg in messages:
                                if msg.buttons:
                                    for row in msg.buttons:
                                        for btn in row:
                                            all_buttons.append(btn)
                                            # فقط دکمه‌هایی که URL دارن و t.me هستن
                                            if hasattr(btn, 'url') and btn.url:
                                                raw = btn.url.strip()
                                                if 't.me/' in raw or raw.startswith('@'):
                                                    normalized = normalize_tme_link(raw)
                                                    if normalized and normalized not in found_links:
                                                        found_links.append(normalized)
                            
                            if not found_links:
                                executed_steps.append(f"⚠️ {action}: هیچ لینک کانالی پیدا نشد")
                            else:
                                label = "auto_join_leave" if is_auto_leave else "auto_join"
                                executed_steps.append(f"🔍 {label}: {len(found_links)} لینک پیدا شد")
                                
                                # جوین به هر لینک
                                joined = 0
                                for link in found_links:
                                    try:
                                        if 'joinchat/' in link or '/+' in link:
                                            # لینک خصوصی — استخراج hash کامل
                                            if '/+' in link:
                                                hash_part = link.split('/+')[-1].strip().rstrip('/')
                                            elif 'joinchat/' in link:
                                                hash_part = link.split('joinchat/')[-1].strip().rstrip('/')
                                            else:
                                                hash_part = link.split('/')[-1].replace('+', '').strip()
                                            
                                            if not hash_part:
                                                executed_steps.append(f"⚠️ hash خالی: {link[:30]}")
                                                continue
                                            
                                            await client(functions.messages.ImportChatInviteRequest(hash_part))
                                        else:
                                            username = link.rstrip('/').split('/')[-1].lstrip('@')
                                            if not username:
                                                continue
                                            
                                            # بررسی اینکه entity کانال/گروه هست نه یوزر
                                            from telethon.tl.types import User, Channel, Chat
                                            try:
                                                entity = await client.get_entity(username)
                                            except Exception:
                                                executed_steps.append(f"⚠️ entity پیدا نشد: {username}")
                                                continue
                                            
                                            if isinstance(entity, User):
                                                executed_steps.append(f"⏭️ skip (یوزر): {username}")
                                                continue
                                            
                                            await client(functions.channels.JoinChannelRequest(entity))
                                        
                                        joined += 1
                                        executed_steps.append(f"✅ جوین: {link.split('/')[-1]}")
                                        if is_auto_leave and link not in auto_leave_channels:
                                            auto_leave_channels.append(link)
                                        await asyncio.sleep(1.5)
                                    except Exception as e:
                                        err = str(e)
                                        if 'already' in err.lower() or 'USER_ALREADY' in err:
                                            executed_steps.append(f"ℹ️ قبلاً عضو: {link.split('/')[-1]}")
                                            joined += 1
                                            if is_auto_leave and link not in auto_leave_channels:
                                                auto_leave_channels.append(link)
                                        elif 'expired' in err.lower() or 'INVITE_HASH_EXPIRED' in err:
                                            executed_steps.append(f"⚠️ لینک منقضی: {link.split('/')[-1][:20]}")
                                        elif 'wait' in err.lower() or 'FLOOD' in err:
                                            # flood wait - صبر کن
                                            import re as _re
                                            wait_match = _re.search(r'(\d+)', err)
                                            wait_sec = int(wait_match.group(1)) if wait_match else 10
                                            executed_steps.append(f"⏳ flood wait {wait_sec}s: {link.split('/')[-1][:20]}")
                                            await asyncio.sleep(min(wait_sec, 30))
                                        else:
                                            executed_steps.append(f"❌ جوین ناموفق {link.split('/')[-1]}: {err[:40]}")
                                
                                suffix = " (لفت خودکار در پایان)" if is_auto_leave else ""
                                executed_steps.append(f"📊 جوین: {joined}/{len(found_links)} موفق{suffix}")
                            
                            # کلیک روی دکمه
                            if mode != 'no_click' and all_buttons:
                                await asyncio.sleep(1)
                                
                                fresh_msg = await client.get_messages(bot, limit=1)
                                if fresh_msg and fresh_msg[0].buttons:
                                    flat_buttons = []
                                    for row in fresh_msg[0].buttons:
                                        for btn in row:
                                            flat_buttons.append(btn)
                                    
                                    btn_to_click = None
                                    
                                    if mode == 'custom_click' and click_text:
                                        for btn in flat_buttons:
                                            btn_txt = btn.text if hasattr(btn, 'text') else ''
                                            clean_btn = ''.join(c for c in btn_txt if c.isalnum() or c.isspace()).strip().lower()
                                            clean_search = ''.join(c for c in click_text if c.isalnum() or c.isspace()).strip().lower()
                                            if clean_search in clean_btn:
                                                btn_to_click = btn
                                                break
                                    else:
                                        btn_to_click = flat_buttons[-1]
                                    
                                    if btn_to_click:
                                        btn_txt = btn_to_click.text if hasattr(btn_to_click, 'text') else '?'
                                        await btn_to_click.click()
                                        executed_steps.append(f"✅ کلیک دکمه: {btn_txt}")
                                    else:
                                        executed_steps.append(f"⚠️ دکمه '{click_text}' پیدا نشد")
                        
                        except Exception as e:
                            logger.error(f"خطا در {action}: {e}")
                            executed_steps.append(f"❌ خطا در {action}: {str(e)[:40]}")
                    
                    elif action == 'join_addlist':
                        # جوین به addlist (لیست کانال‌ها/گروه‌ها)
                        # فرمت: join_addlist: https://t.me/addlist/...
                        addlist_link = value.strip()
                        try:
                            # استخراج slug از لینک
                            # مثال: https://t.me/addlist/BJ1gpHd43ew2MzQx
                            if 'addlist/' in addlist_link:
                                slug = addlist_link.split('addlist/')[-1].strip()
                                
                                # استفاده از CheckChatlistInviteRequest برای چک کردن
                                result = await client(functions.chatlists.CheckChatlistInviteRequest(
                                    slug=slug
                                ))
                                
                                # جوین به addlist
                                await client(functions.chatlists.JoinChatlistInviteRequest(
                                    slug=slug
                                ))
                                
                                executed_steps.append(f"✅ جوین addlist: {addlist_link[:40]}")
                                logger.info(f"جوین موفق به addlist: {slug}")
                            else:
                                executed_steps.append(f"❌ لینک addlist نامعتبر")
                        except Exception as e:
                            executed_steps.append(f"❌ جوین addlist ناموفق: {str(e)[:40]}")
                            logger.error(f"خطا در جوین addlist: {e}")
                    
                    elif action == 'leave_addlist':
                        # خروج از addlist
                        # فرمت: leave_addlist: https://t.me/addlist/...
                        addlist_link = value.strip()
                        try:
                            # استخراج slug از لینک
                            if 'addlist/' in addlist_link:
                                slug = addlist_link.split('addlist/')[-1].strip()
                                
                                # دریافت اطلاعات addlist
                                result = await client(functions.chatlists.CheckChatlistInviteRequest(
                                    slug=slug
                                ))
                                
                                # اگر chatlist_id داشته باشیم، از اون استفاده می‌کنیم
                                # در غیر این صورت باید از slug استفاده کنیم
                                if hasattr(result, 'chatlist') and hasattr(result.chatlist, 'id'):
                                    chatlist_id = result.chatlist.id
                                    
                                    # حذف addlist
                                    await client(functions.chatlists.DeleteChatlistRequest(
                                        chatlist_id=chatlist_id
                                    ))
                                    
                                    executed_steps.append(f"✅ خروج از addlist: {addlist_link[:40]}")
                                    logger.info(f"خروج موفق از addlist: {slug}")
                                else:
                                    executed_steps.append(f"⚠️ addlist پیدا نشد یا قبلاً حذف شده")
                            else:
                                executed_steps.append(f"❌ لینک addlist نامعتبر")
                        except Exception as e:
                            executed_steps.append(f"❌ خروج از addlist ناموفق: {str(e)[:40]}")
                            logger.error(f"خطا در خروج از addlist: {e}")
                    
                    elif action == 'smart_delay':
                        # فعال/غیرفعال کردن صبر هوشمند بعد از هر دستور
                        # فرمت‌ها:
                        #   smart_delay: on        ← فعال با timeout پیش‌فرض 20s
                        #   smart_delay: on, 30    ← فعال با timeout 30s
                        #   smart_delay: off       ← غیرفعال
                        v = value.strip().lower() if value else 'on'
                        if v.startswith('off'):
                            smart_delay_enabled = False
                            executed_steps.append("🔕 صبر هوشمند غیرفعال شد")
                        else:
                            smart_delay_enabled = True
                            parts = [p.strip() for p in v.split(',')]
                            if len(parts) > 1:
                                try:
                                    smart_delay_timeout = int(parts[1])
                                except ValueError:
                                    smart_delay_timeout = 20
                            else:
                                smart_delay_timeout = 20
                            executed_steps.append(f"🔔 صبر هوشمند فعال شد (timeout: {smart_delay_timeout}s)")
                        continue  # تاخیر معمولی نمی‌خواد
                    
                    elif action == 'wait':
                        # صبر کردن
                        wait_time = int(value) if value else delay
                        await asyncio.sleep(wait_time)
                        executed_steps.append(f"⏱ صبر {wait_time} ثانیه")
                    
                    elif action == 'wait_for':
                        # صبر هوشمند - منتظر پیام جدید از ربات می‌مونه
                        # فرمت‌ها:
                        #   wait_for: 30              ← صبر تا هر پیام جدیدی بیاد (max 30s)
                        #   wait_for: 30, کیف پول     ← صبر تا پیام حاوی "کیف پول" بیاد
                        #   wait_for: 30, button      ← صبر تا پیامی با دکمه بیاد
                        try:
                            timeout = 30  # پیش‌فرض
                            keyword = None
                            wait_for_button = False
                            
                            if value:
                                parts = [p.strip() for p in value.split(',', 1)]
                                try:
                                    timeout = int(parts[0])
                                except ValueError:
                                    timeout = 30
                                
                                if len(parts) > 1:
                                    kw = parts[1].strip()
                                    if kw.lower() == 'button':
                                        wait_for_button = True
                                    else:
                                        keyword = kw
                            
                            # دریافت آخرین پیام فعلی برای مقایسه
                            current_msgs = await client.get_messages(bot, limit=1)
                            last_msg_id = current_msgs[0].id if current_msgs else 0
                            
                            # توضیح انتظار
                            if keyword:
                                wait_desc = f"پیام حاوی '{keyword}'"
                            elif wait_for_button:
                                wait_desc = "پیام با دکمه"
                            else:
                                wait_desc = "پیام جدید"
                            
                            executed_steps.append(f"⏳ منتظر {wait_desc} (حداکثر {timeout}s)...")
                            
                            # حلقه انتظار
                            elapsed = 0
                            found = False
                            check_interval = 1  # هر 1 ثانیه چک کن
                            
                            while elapsed < timeout:
                                await asyncio.sleep(check_interval)
                                elapsed += check_interval
                                
                                # دریافت پیام‌های جدید
                                new_msgs = await client.get_messages(bot, limit=3)
                                
                                for msg in new_msgs:
                                    if msg.id <= last_msg_id:
                                        continue  # پیام قدیمیه
                                    
                                    # پیام جدید پیدا شد
                                    if keyword:
                                        # بررسی کلمه کلیدی
                                        msg_text = msg.text or ''
                                        if keyword.lower() in msg_text.lower():
                                            found = True
                                            executed_steps.append(f"✅ پیام با '{keyword}' دریافت شد ({elapsed}s)")
                                            break
                                    elif wait_for_button:
                                        # بررسی وجود دکمه
                                        if msg.buttons:
                                            found = True
                                            executed_steps.append(f"✅ پیام با دکمه دریافت شد ({elapsed}s)")
                                            break
                                    else:
                                        # هر پیام جدیدی کافیه
                                        found = True
                                        executed_steps.append(f"✅ پیام جدید دریافت شد ({elapsed}s)")
                                        break
                                
                                if found:
                                    break
                            
                            if not found:
                                executed_steps.append(f"⚠️ timeout: {wait_desc} در {timeout}s نرسید، ادامه می‌دهیم...")
                        
                        except Exception as e:
                            logger.error(f"خطا در wait_for: {e}")
                            executed_steps.append(f"⚠️ خطا در wait_for: {str(e)[:30]}, ادامه...")
                    
                    elif action == 'stop':
                        # توقف موقت سناریو
                        # فرمت: stop: N (N ثانیه توقف)
                        # یا: stop: (توقف پیش‌فرض 5 ثانیه)
                        stop_time = int(value) if value and value.isdigit() else 5
                        await asyncio.sleep(stop_time)
                        executed_steps.append(f"⏸ توقف {stop_time} ثانیه")
                    
                    elif action == 'solve_captcha':
                        # حل خودکار کپچای ریاضی
                        # فرمت: solve_captcha: send (ارسال جواب به صورت متن)
                        # یا: solve_captcha: click (کلیک روی دکمه با جواب)
                        # یا: solve_captcha: send, 3 (بررسی 3 پیام آخر)
                        try:
                            # تجزیه value برای دریافت mode و limit
                            mode = 'send'
                            message_limit = 1
                            
                            if value:
                                parts = [p.strip() for p in value.split(',')]
                                mode = parts[0].lower() if parts[0] else 'send'
                                
                                # اگر پارامتر دوم وجود داشت، تعداد پیام‌ها
                                if len(parts) > 1 and parts[1].isdigit():
                                    message_limit = int(parts[1])
                                    message_limit = min(message_limit, 10)  # حداکثر 10 پیام
                            
                            # صبر کوتاه برای اطمینان از دریافت پیام کپچا
                            await asyncio.sleep(1)
                            
                            # دریافت چند پیام آخر ربات
                            messages = await client.get_messages(bot, limit=message_limit)
                            
                            if not messages:
                                executed_steps.append(f"⚠️ کپچا: پیامی برای حل پیدا نشد")
                                logger.warning("پیام کپچا پیدا نشد")
                                continue
                            
                            # جستجوی معادله در پیام‌ها
                            found_message = None
                            found_index = -1
                            
                            for idx, msg in enumerate(messages):
                                if msg.text:
                                    # الگوهای مختلف معادلات ریاضی
                                    patterns = [
                                        r'(\d+)\s*\+\s*(\d+)\s*=\s*\?',  # 5 + 3 = ?
                                        r'(\d+)\s*-\s*(\d+)\s*=\s*\?',   # 81 - 4 = ?
                                        r'(\d+)\s*×\s*(\d+)\s*=\s*\?',   # 5 × 3 = ?
                                        r'(\d+)\s*\*\s*(\d+)\s*=\s*\?',  # 5 * 3 = ?
                                        r'(\d+)\s*÷\s*(\d+)\s*=\s*\?',   # 10 ÷ 2 = ?
                                        r'(\d+)\s*/\s*(\d+)\s*=\s*\?',   # 10 / 2 = ?
                                        r'(\d+)\s*\+\s*(\d+)',            # 5 + 3
                                        r'(\d+)\s*-\s*(\d+)',             # 81 - 4
                                        r'(\d+)\s*×\s*(\d+)',             # 5 × 3
                                        r'(\d+)\s*\*\s*(\d+)',            # 5 * 3
                                        r'(\d+)\s*÷\s*(\d+)',             # 10 ÷ 2
                                        r'(\d+)\s*/\s*(\d+)',             # 10 / 2
                                    ]
                                    
                                    # چک کردن هر الگو
                                    for pattern in patterns:
                                        if re.search(pattern, msg.text):
                                            found_message = msg.text
                                            found_index = idx
                                            break
                                    
                                    if found_message:
                                        break
                            
                            if not found_message:
                                executed_steps.append(f"⚠️ کپچا: معادله در {message_limit} پیام آخر پیدا نشد")
                                logger.warning(f"معادله در {message_limit} پیام پیدا نشد")
                                continue
                            
                            # نمایش بخشی از پیام در گزارش
                            message_preview = found_message[:100].replace('\n', ' ')
                            logger.info(f"پیام کپچا (پیام #{found_index + 1}): {found_message}")
                            
                            if message_limit > 1:
                                executed_steps.append(f"🔍 کپچا در پیام #{found_index + 1} از {message_limit} پیام: {message_preview}...")
                            else:
                                executed_steps.append(f"🔍 کپچا دریافت شد: {message_preview}...")
                            
                            # حالا معادله رو حل می‌کنیم
                            answer = None
                            operation = None
                            
                            # الگوهای مختلف معادلات ریاضی
                            patterns = [
                                r'(\d+)\s*\+\s*(\d+)\s*=\s*\?',  # 5 + 3 = ?
                                r'(\d+)\s*-\s*(\d+)\s*=\s*\?',   # 81 - 4 = ?
                                r'(\d+)\s*×\s*(\d+)\s*=\s*\?',   # 5 × 3 = ?
                                r'(\d+)\s*\*\s*(\d+)\s*=\s*\?',  # 5 * 3 = ?
                                r'(\d+)\s*÷\s*(\d+)\s*=\s*\?',   # 10 ÷ 2 = ?
                                r'(\d+)\s*/\s*(\d+)\s*=\s*\?',   # 10 / 2 = ?
                                r'(\d+)\s*\+\s*(\d+)',            # 5 + 3
                                r'(\d+)\s*-\s*(\d+)',             # 81 - 4
                                r'(\d+)\s*×\s*(\d+)',             # 5 × 3
                                r'(\d+)\s*\*\s*(\d+)',            # 5 * 3
                                r'(\d+)\s*÷\s*(\d+)',             # 10 ÷ 2
                                r'(\d+)\s*/\s*(\d+)',             # 10 / 2
                            ]
                            
                            # جستجوی معادله و حل آن
                            for pattern in patterns:
                                match = re.search(pattern, found_message)
                                if match:
                                    num1 = int(match.group(1))
                                    num2 = int(match.group(2))
                                    
                                    # تشخیص نوع عملیات
                                    if '+' in match.group(0):
                                        answer = num1 + num2
                                        operation = f"{num1} + {num2}"
                                    elif '-' in match.group(0):
                                        answer = num1 - num2
                                        operation = f"{num1} - {num2}"
                                    elif '×' in match.group(0) or '*' in match.group(0):
                                        answer = num1 * num2
                                        operation = f"{num1} × {num2}"
                                    elif '÷' in match.group(0) or '/' in match.group(0):
                                        if num2 != 0:
                                            answer = num1 // num2  # تقسیم صحیح
                                            operation = f"{num1} ÷ {num2}"
                                    
                                    break
                            
                            if answer is None:
                                executed_steps.append(f"⚠️ کپچا: معادله ریاضی پیدا نشد در پیام")
                                executed_steps.append(f"   📝 متن پیام: {message_preview}...")
                                logger.warning(f"معادله پیدا نشد در: {found_message}")
                                continue
                            
                            logger.info(f"معادله حل شد: {operation} = {answer}")
                            executed_steps.append(f"🧮 معادله پیدا شد: {operation} = {answer}")
                            
                            # ارسال جواب بر اساس mode
                            if mode == 'send':
                                # ارسال جواب به صورت متن
                                await client.send_message(bot, str(answer))
                                executed_steps.append(f"✅ کپچا حل شد و ارسال شد: {answer}")
                            
                            elif mode == 'click':
                                # کلیک روی دکمه با جواب
                                messages = await client.get_messages(bot, limit=1)
                                
                                if messages and messages[0].buttons:
                                    button_found = False
                                    answer_str = str(answer)
                                    
                                    # لیست دکمه‌ها برای گزارش
                                    all_buttons_text = []
                                    
                                    for row in messages[0].buttons:
                                        for button in row:
                                            button_text = button.text if hasattr(button, 'text') else str(button)
                                            all_buttons_text.append(button_text)
                                            
                                            # جستجوی جواب در متن دکمه
                                            if answer_str in button_text or button_text.strip() == answer_str:
                                                await button.click()
                                                button_found = True
                                                executed_steps.append(f"✅ کپچا حل شد و کلیک شد: دکمه '{button_text}'")
                                                break
                                        
                                        if button_found:
                                            break
                                    
                                    if not button_found:
                                        executed_steps.append(f"⚠️ کپچا: دکمه با جواب '{answer}' پیدا نشد")
                                        executed_steps.append(f"   🔘 دکمه‌های موجود: {', '.join(all_buttons_text)}")
                                else:
                                    executed_steps.append(f"⚠️ کپچا: دکمه‌ای برای کلیک وجود ندارد")
                            
                            else:
                                executed_steps.append(f"❌ کپچا: mode نامعتبر '{mode}' (باید send یا click باشد)")
                        
                        except Exception as e:
                            logger.error(f"خطا در حل کپچا: {e}")
                            executed_steps.append(f"❌ خطا در حل کپچا: {str(e)[:30]}")
                    
                    elif action == 'share_phone' or action == 'share_contact':
                        # اشتراک‌گذاری شماره تماس با ربات
                        # فرمت: share_phone: (بدون value - خودکار شماره اکانت رو میفرسته)
                        try:
                            # دریافت اطلاعات کاربر فعلی
                            me = await client.get_me()
                            
                            # ارسال شماره تماس با استفاده از InputMediaContact
                            from telethon.tl.types import InputMediaContact
                            
                            await client.send_message(
                                bot,
                                file=InputMediaContact(
                                    phone_number=me.phone,
                                    first_name=me.first_name or "User",
                                    last_name=me.last_name or "",
                                    vcard=""
                                )
                            )
                            
                            executed_steps.append(f"✅ شماره تماس به اشتراک گذاشته شد: +{me.phone}")
                            logger.info(f"شماره تماس +{me.phone} با ربات @{bot_username} به اشتراک گذاشته شد")
                            
                        except Exception as e:
                            logger.error(f"خطا در اشتراک‌گذاری شماره: {e}")
                            executed_steps.append(f"❌ خطا در اشتراک‌گذاری شماره: {str(e)[:30]}")
                    
                    elif action == 'forward':
                        # فوروارد پیام‌های اخیر یا پیام خاص
                        # فرمت 1: forward: N, @target (N تا پیام آخر)
                        # فرمت 2: forward: "متن", @target (پیام حاوی متن مشخص)
                        # مثال 1: forward: 5, @mychannel
                        # مثال 2: forward: "لینک شما", @mychannel
                        try:
                            parts = value.split(',', 1)
                            if len(parts) != 2:
                                executed_steps.append(f"❌ فرمت نادرست! استفاده: forward: N, @target یا forward: \"متن\", @target")
                                continue
                            
                            first_part = parts[0].strip()
                            target = parts[1].strip().lstrip('@')
                            
                            # تشخیص نوع: عدد یا متن
                            search_text = None
                            count = None
                            
                            # اگر با " یا ' شروع شده، متن جستجو است
                            if (first_part.startswith('"') and first_part.endswith('"')) or \
                               (first_part.startswith("'") and first_part.endswith("'")):
                                search_text = first_part[1:-1]  # حذف کوتیشن‌ها
                            else:
                                try:
                                    count = int(first_part)
                                except ValueError:
                                    executed_steps.append(f"❌ فرمت نادرست! باید عدد یا \"متن\" باشد")
                                    continue
                            
                            # دریافت entity هدف
                            try:
                                target_entity = await client.get_entity(target)
                            except Exception as e:
                                executed_steps.append(f"❌ هدف '{target}' پیدا نشد: {str(e)[:30]}")
                                continue
                            
                            # دریافت پیام‌ها
                            if search_text:
                                # جستجوی پیام حاوی متن خاص (100 پیام آخر رو چک می‌کنیم)
                                messages = await client.get_messages(bot, limit=100)
                                matching_messages = []
                                
                                for msg in messages:
                                    if msg.text and search_text.lower() in msg.text.lower():
                                        matching_messages.append(msg)
                                
                                if not matching_messages:
                                    executed_steps.append(f"⚠️ پیامی حاوی '{search_text}' پیدا نشد")
                                    continue
                                
                                messages_to_forward = matching_messages
                            else:
                                # دریافت N تا پیام آخر
                                messages = await client.get_messages(bot, limit=count)
                                
                                if not messages:
                                    executed_steps.append(f"⚠️ پیامی برای فوروارد وجود ندارد")
                                    continue
                                
                                messages_to_forward = messages
                            
                            # فوروارد پیام‌ها
                            forwarded_count = 0
                            for msg in reversed(messages_to_forward):  # از قدیمی به جدید
                                try:
                                    await client.forward_messages(target_entity, msg)
                                    forwarded_count += 1
                                    await asyncio.sleep(0.5)  # تاخیر کوچک بین فوروارد
                                except Exception as e:
                                    logger.error(f"خطا در فوروارد پیام: {e}")
                            
                            if search_text:
                                executed_steps.append(f"✅ فوروارد {forwarded_count} پیام حاوی '{search_text}' به @{target}")
                            else:
                                executed_steps.append(f"✅ فوروارد {forwarded_count} پیام به @{target}")
                        
                        except Exception as e:
                            executed_steps.append(f"❌ خطا در فوروارد: {str(e)[:30]}")
                    
                    
                    elif action == 'extract_referral':
                        # استخراج لینک رفرال اختصاصی
                        try:
                            # صبر کوتاه برای دریافت پاسخ احتمالی ربات
                            await asyncio.sleep(2)
                            
                            # دریافت آخرین پیام‌های ربات
                            messages = await client.get_messages(bot, limit=5)
                            ref_code = None
                            
                            for msg in messages:
                                if msg.text:
                                    # جستجوی الگوی لینک دعوت/رفرال تلگرام
                                    match = re.search(r'(?:t\.me|telegram\.me)/[a-zA-Z0-9_]+\?start=([a-zA-Z0-9_-]+)', msg.text)
                                    if match:
                                        ref_code = match.group(1)
                                        break
                            
                            if ref_code:
                                extracted_ref_code = ref_code
                                executed_steps.append(f"✅ کد رفرال اختصاصی استخراج شد: {ref_code}")
                                logger.info(f"کد رفرال استخراج شد: {ref_code}")
                            else:
                                executed_steps.append("⚠️ رفرال: لینک دعوتی در ۵ پیام اخیر ربات یافت نشد")
                                logger.warning("لینک رفرال در پیام‌ها پیدا نشد")
                                
                        except Exception as e:
                            logger.error(f"خطا در استخراج رفرال: {e}")
                            executed_steps.append(f"❌ خطا در استخراج رفرال: {str(e)[:30]}")
                    # تاخیر بین مراحل
                    if smart_delay_enabled and action not in ('wait', 'wait_for', 'stop', 'smart_delay'):
                        # صبر هوشمند: منتظر پیام جدید از ربات
                        try:
                            current_msgs = await client.get_messages(bot, limit=1)
                            last_id = current_msgs[0].id if current_msgs else 0
                            
                            elapsed = 0
                            found = False
                            while elapsed < smart_delay_timeout:
                                await asyncio.sleep(1)
                                elapsed += 1
                                new_msgs = await client.get_messages(bot, limit=1)
                                if new_msgs and new_msgs[0].id > last_id:
                                    found = True
                                    break
                            
                            if not found:
                                # timeout شد، یه تاخیر کوچک بزار و ادامه بده
                                await asyncio.sleep(delay)
                        except Exception:
                            await asyncio.sleep(delay)
                    else:
                        await asyncio.sleep(delay)
                    
                except Exception as e:
                    logger.error(f"خطا در مرحله {step_num}: {e}")
                    executed_steps.append(f"❌ خطا در مرحله {step_num}: {str(e)[:30]}")
            
            # ── لفت خودکار کانال‌های join_leave ─────────────────
            if auto_leave_channels:
                executed_steps.append("─" * 20)
                executed_steps.append("🚪 لفت خودکار کانال‌ها:")
                for channel_link in auto_leave_channels:
                    try:
                        if 'joinchat/' in channel_link or '/+' in channel_link:
                            # لینک خصوصی — جستجو در دیالوگ‌ها با invite hash
                            hash_part = channel_link.split('/')[-1].replace('+', '')
                            left = False
                            
                            # جستجو در دیالوگ‌ها برای پیدا کردن کانال
                            async for dialog in client.iter_dialogs(limit=200):
                                entity = dialog.entity
                                # بررسی invite_hash اگر موجود باشه
                                if hasattr(entity, 'username') and entity.username:
                                    continue  # کانال‌های عمومی رو skip کن
                                # سعی کن با title یا id پیدا کنیم
                                try:
                                    await client(functions.channels.LeaveChannelRequest(entity))
                                    # اگر موفق شد، این کانال بود
                                    executed_steps.append(f"✅ لفت: {dialog.name[:30]}")
                                    left = True
                                    break
                                except Exception:
                                    continue
                            
                            if not left:
                                # روش دوم: از طریق CheckChatInvite
                                try:
                                    invite_info = await client(functions.messages.CheckChatInviteRequest(hash=hash_part))
                                    if hasattr(invite_info, 'chat'):
                                        await client(functions.channels.LeaveChannelRequest(invite_info.chat))
                                        executed_steps.append(f"✅ لفت (invite): {channel_link.split('/')[-1][:20]}")
                                        left = True
                                except Exception:
                                    pass
                            
                            if not left:
                                executed_steps.append(f"⚠️ لفت ناموفق (لینک خصوصی): {channel_link[:30]}")
                        else:
                            username = channel_link.rstrip('/').split('/')[-1].lstrip('@')
                            entity = await client.get_entity(username)
                            await client(functions.channels.LeaveChannelRequest(entity))
                            executed_steps.append(f"✅ لفت: {channel_link.split('/')[-1]}")
                        await asyncio.sleep(1)
                    except Exception as e:
                        executed_steps.append(f"❌ لفت ناموفق: {str(e)[:40]}")
                        logger.error(f"خطا در لفت خودکار {channel_link}: {e}")
            
            return {
                'success': True,
                'message': 'سناریو با موفقیت اجرا شد',
                'bot_username': bot_username,
                'executed_steps': executed_steps,
                'extracted_ref_code': extracted_ref_code,
                'is_new_user': is_new_user
            }
            
        except Exception as e:
            err_str = str(e)
            logger.exception(f"خطا در اجرای سناریو: {e}")
            
            # تشخیص سشن نامعتبر از خطاهای مختلف
            invalid_keywords = [
                'auth_key', 'unauthorized', 'SESSION_REVOKED',
                'USER_DEACTIVATED', 'AUTH_KEY_UNREGISTERED',
                'SESSION_EXPIRED', 'سشن نامعتبر'
            ]
            is_invalid = any(kw.lower() in err_str.lower() for kw in invalid_keywords)
            
            if is_invalid and db:
                logger.warning(f"سشن نامعتبر تشخیص داده شد، غیرفعال می‌شود: {session_path}")
                await db.invalidate_session(session_path)
            
            return {
                'success': False,
                'message': f'خطا: {err_str}',
                'invalid_session': is_invalid
            }
        
        finally:
            if client:
                await client.disconnect()
    
    async def bulk_execute_scenario(self, session_paths: List[str], bot_username: str,
                                    scenario: List[Dict], progress_callback=None, 
                                    cancel_flag: Optional[Dict] = None,
                                    db=None) -> Dict[str, any]:
        """
        اجرای دسته‌جمعی سناریو با قابلیت لغو و مکث
        
        Args:
            session_paths: لیست مسیر فایل‌های سشن
            bot_username: یوزرنیم ربات
            scenario: لیست مراحل سناریو
            progress_callback: تابع callback برای نمایش پیشرفت
            cancel_flag: دیکشنری برای بررسی لغو/مکث عملیات
            db: دیتابیس برای غیرفعال کردن سشن‌های نامعتبر
            
        Returns:
            دیکشنری حاوی نتایج
        """
        results = {
            'success': 0,
            'failed': 0,
            'cancelled': 0,
            'invalid_sessions': 0,
            'details': []
        }
        
        total = len(session_paths)
        
        for index, session_path in enumerate(session_paths, 1):
            # بررسی لغو عملیات
            if cancel_flag and cancel_flag.get('cancelled'):
                logger.info(f"عملیات توسط کاربر لغو شد در مرحله {index}/{total}")
                results['cancelled'] = total - index + 1
                break
            
            # بررسی مکث - صبر تا resume شود
            while cancel_flag and cancel_flag.get('paused'):
                logger.info(f"عملیات در حالت مکث است، صبر می‌کنیم...")
                await asyncio.sleep(1)
                
                if cancel_flag.get('cancelled'):
                    logger.info(f"عملیات در حین مکث لغو شد")
                    results['cancelled'] = total - index + 1
                    return results
            
            # محاسبه تاخیر تصادفی
            delay = Config.DELAY_BETWEEN_ACTIONS + random.randint(0, Config.DELAY_RANDOM_RANGE)
            
            if progress_callback:
                await progress_callback(index, total, f"در حال اجرای سناریو {index}/{total}...")
            
            logger.info(f"اجرای سناریو برای اکانت {index}/{total} - تاخیر: {delay}s")
            
            result = await self.execute_scenario(session_path, bot_username, scenario, db=db)
            
            if result['success']:
                results['success'] += 1
            elif result.get('invalid_session'):
                results['invalid_sessions'] += 1
                results['failed'] += 1
            else:
                results['failed'] += 1
            
            results['details'].append({
                'session': Path(session_path).name,
                'result': result
            })
            
            # تاخیر بین عملیات‌ها
            if index < total:
                logger.info(f"صبر {delay} ثانیه قبل از عملیات بعدی...")
                await asyncio.sleep(delay)
        
        return results
    
    @staticmethod
    def parse_scenario(scenario_text: str) -> List[Dict]:
        """
        تجزیه متن سناریو به لیست مراحل
        
        فرمت:
        start: ref_id
        send: متن پیام
        click: کلمه کلیدی دکمه
        wait: 5
        
        Args:
            scenario_text: متن سناریو
            
        Returns:
            لیست مراحل
        """
        scenario = []
        lines = scenario_text.strip().split('\n')
        
        # دستوراتی که می‌توانند بدون : نوشته شوند
        no_colon_actions = {
            'auto_join', 'auto_join_leave', 'ajl',
            'share_phone', 'share_contact',
            'smart_delay', 'extract_referral'
        }
        no_colon_actions = {
            'auto_join', 'auto_join_leave', 'ajl',
            'share_phone', 'share_contact',
            'smart_delay'
        }
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            if ':' in line:
                action, value = line.split(':', 1)
                action = action.strip().lower()
                value = value.strip()
                
                scenario.append({
                    'action': action,
                    'value': value,
                    'delay': 2
                })
            else:
                # دستوراتی که بدون : نوشته شدن
                action = line.strip().lower()
                if action in no_colon_actions:
                    scenario.append({
                        'action': action,
                        'value': '',
                        'delay': 2
                    })
        
        return scenario
    
    @staticmethod
    def parse_multi_bot_scenario(scenario_text: str) -> List[Dict]:
        """
        تجزیه سناریو چند ربات
        
        فرمت:
        @bot1
        start: ref1
        send: text1
        
        @bot2
        start: ref2
        send: text2
        
        فرمت رفرال چندگانه:
        @bot1
        start: ref1 | 20
        start: ref2 | 30
        start: ref3 | 50
        send: text1
        
        Args:
            scenario_text: متن سناریو
            
        Returns:
            لیست دیکشنری‌ها با bot_username و scenario و referral_distribution
        """
        bots = []
        current_bot = None
        current_scenario = []
        referral_codes = []  # لیست کدهای رفرال با تعداد موفق مورد نیاز
        
        lines = scenario_text.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            
            # خط خالی یا کامنت
            if not line or line.startswith('#'):
                continue
            
            # شروع ربات جدید
            if line.startswith('@'):
                # ذخیره ربات قبلی
                if current_bot and current_scenario:
                    bots.append({
                        'bot_username': current_bot,
                        'scenario': current_scenario,
                        'referral_codes': referral_codes if referral_codes else None
                    })
                
                # شروع ربات جدید
                current_bot = line.lstrip('@')
                current_scenario = []
                referral_codes = []
            
            # دستورات سناریو
            elif current_bot:
                if ':' in line:
                    action, value = line.split(':', 1)
                    action = action.strip().lower()
                    value = value.strip()
                    
                    # بررسی اینکه آیا start با تقسیم‌بندی رفرال است
                    if action == 'start' and '|' in value:
                        # فرمت: ref_code | count
                        parts = value.split('|')
                        ref_code = parts[0].strip()
                        try:
                            target_count = int(parts[1].strip())
                            referral_codes.append({
                                'code': ref_code,
                                'target_count': target_count,
                                'success_count': 0,
                                'failed_count': 0,
                                'accounts_used': []
                            })
                            continue
                        except ValueError:
                            pass
                    
                    current_scenario.append({
                        'action': action,
                        'value': value,
                        'delay': 2
                    })
                
                else:
                    # دستوراتی که بدون : نوشته شدن
                    no_colon_actions = {
                        'auto_join', 'auto_join_leave', 'ajl',
                        'share_phone', 'share_contact',
                        'smart_delay', 'extract_referral'
                    }
                    action = line.strip().lower()
                    if action in no_colon_actions:
                        current_scenario.append({
                            'action': action,
                            'value': '',
                            'delay': 2
                        })
        
        # ذخیره آخرین ربات
        if current_bot and current_scenario:
            bots.append({
                'bot_username': current_bot,
                'scenario': current_scenario,
                'referral_codes': referral_codes if referral_codes else None
            })
        
        return bots
    
    async def execute_multi_bot_scenario(self, session_path: str, 
                                         bots_scenarios: List[Dict],
                                         referral_stats: Optional[Dict] = None,
                                         db=None) -> Dict[str, any]:
        """
        اجرای سناریو چند ربات
        
        Args:
            session_path: مسیر فایل سشن
            bots_scenarios: لیست ربات‌ها و سناریوهایشان
            referral_stats: آمار رفرال‌ها (برای رفرال چندگانه)
            
        Returns:
            دیکشنری حاوی نتایج
        """
        all_results = []
        
        for bot_data in bots_scenarios:
            bot_username = bot_data['bot_username']
            scenario = bot_data['scenario'].copy()  # کپی برای تغییر
            referral_codes = bot_data.get('referral_codes')
            
            # اگر رفرال چندگانه داریم
            if referral_codes and referral_stats:
                # پیدا کردن اولین رفرالی که هنوز به هدف نرسیده
                current_ref = None
                for ref in referral_codes:
                    ref_key = f"{bot_username}_{ref['code']}"
                    if ref_key in referral_stats:
                        stats = referral_stats[ref_key]
                        if stats['success_count'] < stats['target_count']:
                            current_ref = ref['code']
                            break
                
                if current_ref:
                    # اضافه کردن start به ابتدای سناریو
                    scenario.insert(0, {
                        'action': 'start',
                        'value': current_ref,
                        'delay': 2
                    })
                else:
                    # همه رفرال‌ها کامل شدند، از اولین استفاده کن
                    scenario.insert(0, {
                        'action': 'start',
                        'value': referral_codes[0]['code'],
                        'delay': 2
                    })
            
            logger.info(f"اجرای سناریو برای ربات @{bot_username}")
            
            result = await self.execute_scenario(session_path, bot_username, scenario, db=db)
            
            # اگر سشن نامعتبر بود، بقیه ربات‌ها رو هم skip کن
            if result.get('invalid_session'):
                logger.warning(f"سشن نامعتبر در execute_multi_bot_scenario: {session_path}")
                return {
                    'success': False,
                    'message': 'سشن نامعتبر است',
                    'invalid_session': True,
                    'results': all_results
                }
            
            # اگر رفرال چندگانه داریم، آمار رو آپدیت کن
            if referral_codes and referral_stats and current_ref:
                ref_key = f"{bot_username}_{current_ref}"
                if ref_key in referral_stats:
                    if result['success']:
                        referral_stats[ref_key]['success_count'] += 1
                    else:
                        referral_stats[ref_key]['failed_count'] += 1
                    referral_stats[ref_key]['accounts_used'].append(Path(session_path).name)
            
            all_results.append({
                'bot': bot_username,
                'result': result,
                'referral_code': current_ref if referral_codes else None
            })
            
            # تاخیر بین رباتها
            await asyncio.sleep(2)
        
        return {
            'success': all([r['result']['success'] for r in all_results]),
            'message': f"اجرای {len(all_results)} ربات",
            'results': all_results
        }
    
    async def bulk_execute_with_referral_distribution(self, session_paths: List[str],
                                                       bots_scenarios: List[Dict],
                                                       progress_callback=None,
                                                       cancel_flag: Optional[Dict] = None) -> Dict[str, any]:
        """
        اجرای دسته‌جمعی با تقسیم‌بندی رفرال
        
        Args:
            session_paths: لیست مسیر فایل‌های سشن
            bots_scenarios: لیست ربات‌ها و سناریوهایشان
            progress_callback: تابع callback برای نمایش پیشرفت
            cancel_flag: دیکشنری برای بررسی لغو/مکث عملیات
            
        Returns:
            دیکشنری حاوی نتایج و آمار رفرال
        """
        # ایجاد دیکشنری آمار رفرال
        referral_stats = {}
        has_referral_distribution = False
        
        for bot_data in bots_scenarios:
            bot_username = bot_data['bot_username']
            referral_codes = bot_data.get('referral_codes')
            
            if referral_codes:
                has_referral_distribution = True
                for ref in referral_codes:
                    ref_key = f"{bot_username}_{ref['code']}"
                    referral_stats[ref_key] = {
                        'bot': bot_username,
                        'code': ref['code'],
                        'target_count': ref['target_count'],
                        'success_count': 0,
                        'failed_count': 0,
                        'accounts_used': []
                    }
        
        results = {
            'success': 0,
            'failed': 0,
            'cancelled': 0,
            'details': [],
            'referral_stats': referral_stats if has_referral_distribution else None
        }
        
        total = len(session_paths)
        account_index = 0
        
        # اگر رفرال چندگانه داریم، تا زمانی که همه رفرال‌ها کامل نشدن ادامه بده
        while account_index < total:
            # بررسی لغو عملیات
            if cancel_flag and cancel_flag.get('cancelled'):
                logger.info(f"عملیات توسط کاربر لغو شد")
                results['cancelled'] = total - account_index
                break
            
            # بررسی مکث
            while cancel_flag and cancel_flag.get('paused'):
                logger.info(f"عملیات در حالت مکث است...")
                await asyncio.sleep(1)
                
                if cancel_flag.get('cancelled'):
                    logger.info(f"عملیات در حین مکث لغو شد")
                    results['cancelled'] = total - account_index
                    return results
            
            # اگر رفرال چندگانه داریم، بررسی کن که آیا همه کامل شدند
            if has_referral_distribution:
                all_completed = all(
                    stats['success_count'] >= stats['target_count']
                    for stats in referral_stats.values()
                )
                if all_completed:
                    logger.info("همه رفرال‌ها به هدف رسیدند")
                    break
            
            session_path = session_paths[account_index]
            
            # محاسبه تاخیر تصادفی
            delay = Config.DELAY_BETWEEN_ACTIONS + random.randint(0, Config.DELAY_RANDOM_RANGE)
            
            # اگر callback داریم، پیشرفت رو نمایش بدیم
            if progress_callback:
                await progress_callback(account_index + 1, total, f"در حال اجرای اکانت {account_index + 1}/{total}...")
            
            logger.info(f"اجرای سناریو برای اکانت {account_index + 1}/{total}")
            
            result = await self.execute_multi_bot_scenario(
                session_path, bots_scenarios, referral_stats if has_referral_distribution else None
            )
            
            if result['success']:
                results['success'] += 1
            else:
                results['failed'] += 1
            
            results['details'].append({
                'session': Path(session_path).name,
                'result': result
            })
            
            account_index += 1
            
            # تاخیر بین عملیات‌ها
            if account_index < total:
                logger.info(f"صبر {delay} ثانیه قبل از عملیات بعدی...")
                await asyncio.sleep(delay)
        
        return results
    
    async def bulk_execute_multi_bot_scenario(self, session_paths: List[str],
                                              bots_scenarios: List[Dict],
                                              progress_callback=None,
                                              cancel_flag: Optional[Dict] = None,
                                              db=None) -> Dict[str, any]:
        """
        اجرای دسته‌جمعی سناریو چند ربات با قابلیت لغو و مکث
        
        Args:
            session_paths: لیست مسیر فایل‌های سشن
            bots_scenarios: لیست ربات‌ها و سناریوهایشان
            progress_callback: تابع callback برای نمایش پیشرفت
            cancel_flag: دیکشنری برای بررسی لغو/مکث عملیات
            db: دیتابیس برای غیرفعال کردن سشن‌های نامعتبر
            
        Returns:
            دیکشنری حاوی نتایج
        """
        results = {
            'success': 0,
            'failed': 0,
            'cancelled': 0,
            'invalid_sessions': 0,
            'details': []
        }
        
        total = len(session_paths)
        
        for index, session_path in enumerate(session_paths, 1):
            if cancel_flag and cancel_flag.get('cancelled'):
                logger.info(f"عملیات توسط کاربر لغو شد در مرحله {index}/{total}")
                results['cancelled'] = total - index + 1
                break
            
            while cancel_flag and cancel_flag.get('paused'):
                logger.info(f"عملیات در حالت مکث است، صبر می‌کنیم...")
                await asyncio.sleep(1)
                
                if cancel_flag.get('cancelled'):
                    logger.info(f"عملیات در حین مکث لغو شد")
                    results['cancelled'] = total - index + 1
                    return results
            
            delay = Config.DELAY_BETWEEN_ACTIONS + random.randint(0, Config.DELAY_RANDOM_RANGE)
            
            if progress_callback:
                await progress_callback(index, total, f"در حال اجرای سناریو {index}/{total}...")
            
            logger.info(f"اجرای سناریو چند ربات برای اکانت {index}/{total}")
            
            result = await self.execute_multi_bot_scenario(session_path, bots_scenarios, db=db)
            
            if result['success']:
                results['success'] += 1
            elif result.get('invalid_session'):
                results['invalid_sessions'] += 1
                results['failed'] += 1
            else:
                results['failed'] += 1
            
            results['details'].append({
                'session': Path(session_path).name,
                'result': result
            })
            
            if index < total:
                logger.info(f"صبر {delay} ثانیه قبل از عملیات بعدی...")
                await asyncio.sleep(delay)
        
        return results
