"""ماژول مدیریت کانال‌ها و گروه‌ها"""
import logging
import re
import asyncio
import random
from typing import Optional, Dict, List
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from pathlib import Path

from src.config import Config

logger = logging.getLogger(__name__)

class ChannelManager:
    """کلاس مدیریت جوین و لفت کانال‌ها"""
    
    def __init__(self, api_id: Optional[int] = None, api_hash: Optional[str] = None):
        """مقداردهی اولیه"""
        self.api_id = api_id or Config.API_ID
        self.api_hash = api_hash or Config.API_HASH
    
    def parse_channel_link(self, link: str) -> Dict[str, str]:
        """
        تجزیه لینک کانال/گروه
        
        Args:
            link: لینک کانال یا گروه
            
        Returns:
            دیکشنری حاوی نوع و شناسه
        """
        link = link.strip()
        
        # لینک عمومی: https://t.me/channel_username
        public_pattern = r'(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]+)'
        match = re.match(public_pattern, link)
        if match:
            username = match.group(1)
            # حذف joinchat از یوزرنیم
            if username.lower() != 'joinchat':
                return {'type': 'public', 'username': username}
        
        # لینک خصوصی: https://t.me/+hash یا https://t.me/joinchat/hash
        private_pattern = r'(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([a-zA-Z0-9_-]+)'
        match = re.match(private_pattern, link)
        if match:
            hash_code = match.group(1)
            return {'type': 'private', 'hash': hash_code}
        
        # فقط یوزرنیم: @channel_username یا channel_username
        if link.startswith('@'):
            return {'type': 'public', 'username': link[1:]}
        elif not link.startswith('http'):
            return {'type': 'public', 'username': link}
        
        return {'type': 'unknown', 'link': link}
    
    async def join_channel(self, session_path: str, channel_link: str) -> Dict[str, any]:
        """
        جوین کانال/گروه
        
        Args:
            session_path: مسیر فایل سشن
            channel_link: لینک کانال یا گروه
            
        Returns:
            دیکشنری حاوی وضعیت و پیام
        """
        client = None
        
        try:
            # بارگذاری سشن
            from telethon.sessions import StringSession
            session_string = Path(session_path).read_text(encoding='utf-8')
            
            client = TelegramClient(
                StringSession(session_string),
                self.api_id,
                self.api_hash
            )
            
            await client.connect()
            
            if not await client.is_user_authorized():
                return {
                    'success': False,
                    'message': 'سشن نامعتبر است'
                }
            
            # تجزیه لینک
            parsed = self.parse_channel_link(channel_link)
            
            if parsed['type'] == 'unknown':
                return {
                    'success': False,
                    'message': 'فرمت لینک نامعتبر است'
                }
            
            # جوین کانال
            if parsed['type'] == 'public':
                # کانال عمومی
                username = parsed['username']
                logger.info(f"جوین کانال عمومی: {username}")
                
                entity = await client.get_entity(username)
                await client(JoinChannelRequest(entity))
                
                return {
                    'success': True,
                    'message': f'با موفقیت به {entity.title} جوین شدید',
                    'channel_title': entity.title,
                    'channel_username': username
                }
            
            elif parsed['type'] == 'private':
                # کانال خصوصی
                hash_code = parsed['hash']
                logger.info(f"جوین کانال خصوصی: {hash_code}")
                
                updates = await client(ImportChatInviteRequest(hash_code))
                
                # دریافت اطلاعات کانال
                if hasattr(updates, 'chats') and updates.chats:
                    chat = updates.chats[0]
                    return {
                        'success': True,
                        'message': f'با موفقیت به {chat.title} جوین شدید',
                        'channel_title': chat.title
                    }
                else:
                    return {
                        'success': True,
                        'message': 'با موفقیت جوین شدید'
                    }
            
        except errors.UserAlreadyParticipantError:
            logger.info("کاربر قبلاً عضو است")
            return {
                'success': True,
                'message': 'شما قبلاً عضو این کانال/گروه هستید'
            }
        
        except errors.InviteHashExpiredError:
            logger.error("لینک دعوت منقضی شده")
            return {
                'success': False,
                'message': 'لینک دعوت منقضی شده است'
            }
        
        except errors.InviteHashInvalidError:
            logger.error("لینک دعوت نامعتبر")
            return {
                'success': False,
                'message': 'لینک دعوت نامعتبر است'
            }
        
        except errors.ChannelPrivateError:
            logger.error("کانال خصوصی است")
            return {
                'success': False,
                'message': 'این کانال خصوصی است و نیاز به دعوت دارد'
            }
        
        except errors.UsernameNotOccupiedError:
            logger.error("یوزرنیم وجود ندارد")
            return {
                'success': False,
                'message': 'این یوزرنیم وجود ندارد'
            }
        
        except errors.FloodWaitError as e:
            logger.error(f"محدودیت زمانی: {e.seconds} ثانیه")
            return {
                'success': False,
                'message': f'لطفاً {e.seconds} ثانیه صبر کنید'
            }
        
        except Exception as e:
            logger.exception(f"خطا در جوین: {e}")
            return {
                'success': False,
                'message': f'خطا: {str(e)}'
            }
        
        finally:
            if client:
                await client.disconnect()
    
    async def leave_channel(self, session_path: str, channel_link: str) -> Dict[str, any]:
        """
        لفت کانال/گروه
        
        Args:
            session_path: مسیر فایل سشن
            channel_link: لینک کانال یا گروه
            
        Returns:
            دیکشنری حاوی وضعیت و پیام
        """
        client = None
        
        try:
            # بارگذاری سشن
            from telethon.sessions import StringSession
            session_string = Path(session_path).read_text(encoding='utf-8')
            
            client = TelegramClient(
                StringSession(session_string),
                self.api_id,
                self.api_hash
            )
            
            await client.connect()
            
            if not await client.is_user_authorized():
                return {
                    'success': False,
                    'message': 'سشن نامعتبر است'
                }
            
            # تجزیه لینک
            parsed = self.parse_channel_link(channel_link)
            
            if parsed['type'] == 'unknown':
                return {
                    'success': False,
                    'message': 'فرمت لینک نامعتبر است'
                }
            
            entity = None
            
            # دریافت entity
            if parsed['type'] == 'public':
                username = parsed['username']
                try:
                    entity = await client.get_entity(username)
                except Exception as e:
                    logger.error(f"خطا در دریافت entity: {e}")
                    return {
                        'success': False,
                        'message': 'کانال/گروه پیدا نشد'
                    }
            else:
                # برای لینک خصوصی، از hash استفاده می‌کنیم
                # ابتدا سعی می‌کنیم از طریق ImportChatInvite اطلاعات رو بگیریم
                hash_code = parsed['hash']
                
                # جستجو در دیالوگ‌ها برای پیدا کردن کانال
                logger.info(f"جستجو در دیالوگ‌ها برای hash: {hash_code}")
                
                async for dialog in client.iter_dialogs():
                    # چک کردن اینکه آیا این دیالوگ همون کانالی است که جوین شدیم
                    if hasattr(dialog.entity, 'id'):
                        # اگر entity پیدا شد، از آن استفاده می‌کنیم
                        # برای لینک خصوصی، باید از invite link استفاده کنیم
                        try:
                            # سعی می‌کنیم با استفاده از hash، entity رو پیدا کنیم
                            from telethon.tl.functions.messages import CheckChatInviteRequest
                            invite_info = await client(CheckChatInviteRequest(hash_code))
                            
                            if hasattr(invite_info, 'chat'):
                                entity = invite_info.chat
                                break
                        except:
                            continue
                
                if not entity:
                    # اگر پیدا نشد، از آخرین کانالی که جوین شده استفاده می‌کنیم
                    # یا از کاربر بخواهیم یوزرنیم بده
                    logger.warning("نتوانستیم entity را از لینک خصوصی پیدا کنیم")
                    
                    # سعی می‌کنیم مستقیماً با hash لفت کنیم
                    try:
                        from telethon.tl.functions.messages import ImportChatInviteRequest
                        updates = await client(ImportChatInviteRequest(hash_code))
                        
                        if hasattr(updates, 'chats') and updates.chats:
                            entity = updates.chats[0]
                    except errors.UserAlreadyParticipantError:
                        # کاربر قبلاً عضو است، پس می‌تونیم لفت کنیم
                        # باید entity رو از دیالوگ‌ها پیدا کنیم
                        pass
                    except:
                        pass
                    
                    if not entity:
                        return {
                            'success': False,
                            'message': 'برای لفت، لطفاً یوزرنیم کانال را ارسال کنید (مثال: @channel)'
                        }
            
            # لفت کانال
            if entity:
                logger.info(f"لفت کانال: {entity.title if hasattr(entity, 'title') else 'Unknown'}")
                await client(LeaveChannelRequest(entity))
                
                return {
                    'success': True,
                    'message': f'با موفقیت از {entity.title if hasattr(entity, "title") else "کانال/گروه"} خارج شدید',
                    'channel_title': entity.title if hasattr(entity, 'title') else None
                }
            else:
                return {
                    'success': False,
                    'message': 'خطا در پیدا کردن کانال/گروه'
                }
        
        except errors.UserNotParticipantError:
            logger.info("کاربر عضو نیست")
            return {
                'success': True,
                'message': 'شما عضو این کانال/گروه نیستید'
            }
        
        except errors.ChannelPrivateError:
            logger.error("کانال خصوصی است")
            return {
                'success': False,
                'message': 'دسترسی به این کانال وجود ندارد'
            }
        
        except Exception as e:
            logger.exception(f"خطا در لفت: {e}")
            return {
                'success': False,
                'message': f'خطا: {str(e)}'
            }
        
        finally:
            if client:
                await client.disconnect()
    
    async def bulk_join(self, session_paths: List[str], channel_link: str, 
                       progress_callback=None, workers: int = 1, custom_delay: int = None) -> Dict[str, any]:
        """
        جوین دسته‌جمعی با چند اکانت (با تاخیر و تایمر)
        
        Args:
            session_paths: لیست مسیر فایل‌های سشن
            channel_link: لینک کانال یا گروه
            progress_callback: تابع callback برای نمایش پیشرفت
            workers: تعداد اکانت‌های همزمان
            custom_delay: تاخیر سفارشی (None = استفاده از تاخیر پیش‌فرض)
            
        Returns:
            دیکشنری حاوی نتایج
        """
        results = {
            'success': 0,
            'failed': 0,
            'details': []
        }
        
        total = len(session_paths)
        
        # اجرای همزمان با worker
        for i in range(0, total, workers):
            batch = session_paths[i:i + workers]
            tasks = []
            
            for session_path in batch:
                tasks.append(self.join_channel(session_path, channel_link))
            
            # اجرای همزمان batch
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # پردازش نتایج
            for j, result in enumerate(batch_results):
                index = i + j + 1
                
                if isinstance(result, Exception):
                    result = {'success': False, 'message': str(result)}
                
                if result['success']:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                
                results['details'].append({
                    'session': Path(batch[j]).name,
                    'result': result
                })
                
                # بروزرسانی پیشرفت
                if progress_callback:
                    await progress_callback(index, total, f"در حال جوین اکانت {index}/{total}...")
            
            # تاخیر بین batch‌ها (به جز آخرین batch)
            if i + workers < total:
                if custom_delay is not None:
                    delay = custom_delay
                else:
                    delay = Config.DELAY_BETWEEN_ACTIONS + random.randint(0, Config.DELAY_RANDOM_RANGE)
                
                logger.info(f"صبر {delay} ثانیه قبل از batch بعدی...")
                await asyncio.sleep(delay)
        
        return results
    
    async def bulk_leave(self, session_paths: List[str], channel_link: str,
                        progress_callback=None, workers: int = 1, custom_delay: int = None) -> Dict[str, any]:
        """
        لفت دسته‌جمعی با چند اکانت (با تاخیر و تایمر)
        
        Args:
            session_paths: لیست مسیر فایل‌های سشن
            channel_link: لینک کانال یا گروه
            progress_callback: تابع callback برای نمایش پیشرفت
            workers: تعداد اکانت‌های همزمان
            custom_delay: تاخیر سفارشی (None = استفاده از تاخیر پیش‌فرض)
            
        Returns:
            دیکشنری حاوی نتایج
        """
        results = {
            'success': 0,
            'failed': 0,
            'details': []
        }
        
        total = len(session_paths)
        
        # اجرای همزمان با worker
        for i in range(0, total, workers):
            batch = session_paths[i:i + workers]
            tasks = []
            
            for session_path in batch:
                tasks.append(self.leave_channel(session_path, channel_link))
            
            # اجرای همزمان batch
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # پردازش نتایج
            for j, result in enumerate(batch_results):
                index = i + j + 1
                
                if isinstance(result, Exception):
                    result = {'success': False, 'message': str(result)}
                
                if result['success']:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                
                results['details'].append({
                    'session': Path(batch[j]).name,
                    'result': result
                })
                
                # بروزرسانی پیشرفت
                if progress_callback:
                    await progress_callback(index, total, f"در حال لفت اکانت {index}/{total}...")
            
            # تاخیر بین batch‌ها (به جز آخرین batch)
            if i + workers < total:
                if custom_delay is not None:
                    delay = custom_delay
                else:
                    delay = Config.DELAY_BETWEEN_ACTIONS + random.randint(0, Config.DELAY_RANDOM_RANGE)
                
                logger.info(f"صبر {delay} ثانیه قبل از batch بعدی...")
                await asyncio.sleep(delay)
        
        return results

    async def leave_all_channels(self, session_path: str) -> Dict[str, any]:
        """
        لفت از تمام کانال‌ها و گروه‌هایی که اکانت عضوشونه
        
        Args:
            session_path: مسیر فایل سشن
            
        Returns:
            دیکشنری حاوی وضعیت، تعداد لفت موفق و ناموفق
        """
        from telethon.sessions import StringSession
        from telethon.tl.types import Channel, Chat
        
        client = None
        
        try:
            session_string = Path(session_path).read_text(encoding='utf-8')
            
            client = TelegramClient(
                StringSession(session_string),
                self.api_id,
                self.api_hash
            )
            
            await client.connect()
            
            if not await client.is_user_authorized():
                return {
                    'success': False,
                    'message': 'سشن نامعتبر است',
                    'invalid_session': True,
                    'left': 0,
                    'failed': 0
                }
            
            left = 0
            failed = 0
            skipped = 0
            
            # گرفتن همه دیالوگ‌ها
            dialogs = await client.get_dialogs()
            
            for dialog in dialogs:
                entity = dialog.entity
                
                # فقط کانال‌ها و گروه‌ها — چت خصوصی رو رد کن
                if not isinstance(entity, (Channel, Chat)):
                    skipped += 1
                    continue
                
                title = getattr(entity, 'title', '?')
                
                try:
                    if isinstance(entity, Channel):
                        # کانال یا سوپرگروه — از LeaveChannelRequest استفاده کن
                        await client(LeaveChannelRequest(entity))
                    else:
                        # گروه معمولی (Chat) — از DeleteChatUserRequest استفاده کن
                        from telethon.tl.functions.messages import DeleteChatUserRequest
                        me = await client.get_me()
                        await client(DeleteChatUserRequest(chat_id=entity.id, user_id=me.id))
                    
                    left += 1
                    logger.info(f"لفت موفق: {title}")
                    await asyncio.sleep(0.8)  # تاخیر کوچک بین هر لفت
                    
                except errors.UserNotParticipantError:
                    skipped += 1
                    
                except errors.ChatAdminRequiredError:
                    # ادمین است، نمی‌تونه لفت کنه
                    failed += 1
                    logger.warning(f"ادمین کانال است، نمی‌توان لفت کرد: {title}")
                    
                except errors.FloodWaitError as e:
                    logger.warning(f"FloodWait {e.seconds}s در لفت {title}")
                    await asyncio.sleep(min(e.seconds, 30))
                    # دوباره تلاش کن
                    try:
                        await client(LeaveChannelRequest(entity))
                        left += 1
                    except Exception:
                        failed += 1
                        
                except Exception as e:
                    failed += 1
                    logger.warning(f"لفت ناموفق {title}: {e}")
            
            return {
                'success': True,
                'message': f'لفت از {left} کانال/گروه | ناموفق: {failed}',
                'left': left,
                'failed': failed,
                'skipped': skipped
            }
            
        except Exception as e:
            logger.exception(f"خطا در leave_all_channels: {e}")
            return {
                'success': False,
                'message': f'خطا: {str(e)}',
                'left': 0,
                'failed': 0
            }
            
        finally:
            if client:
                await client.disconnect()
    
    async def bulk_leave_all(self, session_paths: List[str],
                             progress_callback=None, workers: int = 1,
                             custom_delay: int = None) -> Dict[str, any]:
        """
        لفت دسته‌جمعی از همه کانال‌ها برای چند اکانت
        
        Args:
            session_paths: لیست مسیر فایل‌های سشن
            progress_callback: تابع callback برای نمایش پیشرفت
            workers: تعداد اکانت‌های همزمان
            custom_delay: تاخیر سفارشی بین اکانت‌ها
            
        Returns:
            دیکشنری حاوی نتایج کلی
        """
        results = {
            'success': 0,
            'failed': 0,
            'details': [],
            'total_left': 0
        }
        
        total = len(session_paths)
        
        for i in range(0, total, workers):
            batch = session_paths[i:i + workers]
            tasks = [self.leave_all_channels(sp) for sp in batch]
            
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for j, result in enumerate(batch_results):
                index = i + j + 1
                
                if isinstance(result, Exception):
                    result = {'success': False, 'message': str(result), 'left': 0, 'failed': 0}
                
                if result['success']:
                    results['success'] += 1
                    results['total_left'] += result.get('left', 0)
                else:
                    results['failed'] += 1
                
                results['details'].append({
                    'session': Path(batch[j]).name,
                    'result': result
                })
                
                if progress_callback:
                    left_count = result.get('left', 0)
                    await progress_callback(
                        index, total,
                        f"اکانت {index}/{total} — لفت از {left_count} کانال/گروه"
                    )
            
            if i + workers < total:
                if custom_delay is not None:
                    delay = custom_delay
                else:
                    delay = Config.DELAY_BETWEEN_ACTIONS + random.randint(0, Config.DELAY_RANDOM_RANGE)
                
                logger.info(f"صبر {delay} ثانیه قبل از اکانت بعدی...")
                await asyncio.sleep(delay)
        
        return results
