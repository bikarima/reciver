"""سرویس پیشی — انتقال موجودی میویی"""
import re
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, List

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Message

from src.config import Config

logger = logging.getLogger(__name__)


def parse_message_link(link: str):
    """
    تجزیه لینک پیام تلگرام
    مثال: https://t.me/meavmeacv/113309
    Returns: (group_username, message_id) or None
    """
    pattern = r'(?:https?://)?t\.me/([^/]+)/(\d+)'
    m = re.match(pattern, link.strip())
    if m:
        return m.group(1), int(m.group(2))
    return None, None


def parse_mio_balance(text: str) -> Optional[int]:
    """
    استخراج میو پوینت از پیام ربات پیشی
    مثال متن: ─ 💰 میو پوینت ها : 8,806 🪙
    Returns: عدد موجودی یا None
    """
    if not text:
        return None

    patterns = [
        r'میو پوینت\s*ها?\s*:\s*([\d,،]+)',   # میو پوینت ها : 8,806
        r'💰[^\n]*:\s*([\d,،]+)',               # 💰 ... : 8806
        r'([\d,،]+)\s*🪙',                      # 8,806 🪙
        r'میو پوینت[^\d]*([\d,،]+)',            # میو پوینت ... 8806
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(',', '').replace('،', '')
            try:
                return int(raw)
            except ValueError:
                continue
    return None


class PishiService:
    """سرویس عملیات ربات پیشی"""

    def __init__(self, api_id: Optional[int] = None, api_hash: Optional[str] = None):
        self.api_id = api_id or Config.API_ID
        self.api_hash = api_hash or Config.API_HASH

    async def transfer_balance(
        self,
        session_path: str,
        group_username: str,
        target_message_id: int,
        wait_timeout: int = 30,
    ) -> Dict:
        """
        انتقال کامل موجودی یک اکانت:
        1. ارسال 'میوهام' در گروه
        2. دریافت reply ربات و parse موجودی
        3. reply به پیام target: 'انتقال موجودی میویی X'

        Returns: {'success': bool, 'balance': int, 'message': str}
        """
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
                return {'success': False, 'balance': 0, 'message': 'سشن نامعتبر است', 'invalid_session': True}

            # دریافت entity گروه
            try:
                group = await client.get_entity(group_username.lstrip('@'))
            except Exception as e:
                return {'success': False, 'balance': 0, 'message': f'گروه پیدا نشد: {e}'}

            me = await client.get_me()

            # --- مرحله ۱: ارسال 'میوهام' ---
            try:
                sent = await client.send_message(group, 'میوهام')
            except Exception as e:
                err = str(e)
                if 'banned' in err.lower() or 'UserBannedInChannel' in err:
                    return {'success': False, 'balance': 0, 'message': 'اکانت در گروه ban شده است'}
                raise
            sent_id = sent.id
            logger.info(f"[{session_path}] ارسال میوهام — msg_id={sent_id}")

            # --- مرحله ۲: منتظر reply ربات ---
            balance = None
            elapsed = 0
            check_interval = 2

            while elapsed < wait_timeout:
                await asyncio.sleep(check_interval)
                elapsed += check_interval

                # دریافت پیام‌های جدید بعد از پیام ما
                msgs = await client.get_messages(group, limit=15, min_id=sent_id - 1)

                logger.info(f"[{session_path}] چک پیام‌ها (elapsed={elapsed}s): {len(msgs)} پیام دریافت شد")

                for msg in msgs:
                    if msg.id <= sent_id:
                        continue  # پیام خودمونه یا قدیمی‌تر

                    reply_to_id = None
                    if msg.reply_to:
                        reply_to_id = getattr(msg.reply_to, 'reply_to_msg_id', None)

                    logger.info(
                        f"[{session_path}] پیام id={msg.id} "
                        f"from={getattr(msg.sender_id, '__str__', lambda: msg.sender_id)()} "
                        f"reply_to={reply_to_id} "
                        f"text={repr((msg.text or '')[:80])}"
                    )

                    if not msg.text:
                        continue

                    # reply به پیام ما باشه
                    is_reply_to_us = reply_to_id == sent_id
                    if not is_reply_to_us:
                        continue

                    parsed = parse_mio_balance(msg.text)
                    logger.info(f"[{session_path}] parse نتیجه: {parsed} از متن: {repr(msg.text[:100])}")
                    if parsed is not None:
                        balance = parsed
                        logger.info(f"[{session_path}] موجودی دریافت شد: {balance}")
                        break

                if balance is not None:
                    break

            if balance is None:
                return {
                    'success': False,
                    'balance': 0,
                    'message': f'موجودی دریافت نشد (timeout {wait_timeout}s)'
                }

            if balance == 0:
                return {
                    'success': True,
                    'balance': 0,
                    'message': 'موجودی صفر است، انتقال انجام نشد'
                }

            # --- مرحله ۳: reply به پیام target ---
            transfer_text = f'انتقال میویی {balance}'
            transfer_sent = await client.send_message(
                group,
                transfer_text,
                reply_to=target_message_id
            )
            transfer_sent_id = transfer_sent.id
            logger.info(f"[{session_path}] ارسال دستور انتقال {balance} — msg_id={transfer_sent_id}")

            # --- مرحله ۴: منتظر reply ربات به پیام انتقال و کلیک دکمه #0 ---
            confirm_elapsed = 0
            confirm_timeout = 20
            confirmed = False

            while confirm_elapsed < confirm_timeout:
                await asyncio.sleep(2)
                confirm_elapsed += 2

                new_msgs = await client.get_messages(group, limit=10, min_id=transfer_sent_id)
                for msg in new_msgs:
                    if msg.id <= transfer_sent_id:
                        continue
                    if not msg.buttons:
                        continue

                    # reply به پیام انتقال ما باشه
                    reply_to_id = getattr(getattr(msg, 'reply_to', None), 'reply_to_msg_id', None)
                    if reply_to_id != transfer_sent_id:
                        continue

                    logger.info(f"[{session_path}] پیام تایید پیدا شد id={msg.id}, کلیک دکمه #0")
                    try:
                        all_buttons = []
                        for row in msg.buttons:
                            for btn in row:
                                all_buttons.append(btn)
                        if all_buttons:
                            await all_buttons[0].click()
                            confirmed = True
                            logger.info(f"[{session_path}] دکمه تایید کلیک شد ✅")
                    except Exception as e:
                        logger.warning(f"[{session_path}] خطا در کلیک دکمه تایید: {e}")
                    break

                if confirmed:
                    break

            if not confirmed:
                logger.warning(f"[{session_path}] پیام تایید در {confirm_timeout}s پیدا نشد")

            # --- مرحله ۵: منتظر تایید نهایی (edit پیام ربات) ---
            final_success = False
            if confirmed:
                await asyncio.sleep(3)
                # پیام ربات edit میشه — دوباره می‌خونیم
                try:
                    check_msgs = await client.get_messages(group, limit=10, min_id=transfer_sent_id)
                    for msg in check_msgs:
                        reply_to_id = getattr(getattr(msg, 'reply_to', None), 'reply_to_msg_id', None)
                        if reply_to_id != transfer_sent_id:
                            continue
                        msg_text = msg.text or ''
                        if 'با موفقیت انتقال یافت' in msg_text or '✅' in msg_text:
                            final_success = True
                            logger.info(f"[{session_path}] انتقال با موفقیت تایید شد ✅")
                            break
                except Exception as e:
                    logger.warning(f"[{session_path}] خطا در چک تایید نهایی: {e}")

            return {
                'success': True,
                'balance': balance,
                'confirmed': confirmed,
                'final_success': final_success,
                'message': (
                    f'انتقال {balance:,} میو پوینت با موفقیت تایید شد ✅'
                    if final_success else
                    f'انتقال {balance:,} میو پوینت ارسال شد'
                    + (' — دکمه تایید کلیک شد' if confirmed else ' — دکمه تایید پیدا نشد')
                )
            }

        except Exception as e:
            logger.exception(f"خطا در transfer_balance: {e}")
            return {'success': False, 'balance': 0, 'message': f'خطا: {str(e)}'}

        finally:
            if client:
                await client.disconnect()

    async def bulk_transfer(
        self,
        session_paths: List[str],
        group_username: str,
        target_message_id: int,
        progress_callback=None,
        workers: int = 1,
        custom_delay: int = None,
        cancel_flag: dict = None,
    ) -> Dict:
        """
        انتقال دسته‌جمعی برای چند اکانت
        """
        import random

        results = {
            'success': 0,
            'failed': 0,
            'total_transferred': 0,
            'details': []
        }
        total = len(session_paths)

        for i in range(0, total, workers):
            if cancel_flag and cancel_flag.get('cancelled'):
                break

            batch = session_paths[i:i + workers]
            tasks = [
                self.transfer_balance(sp, group_username, target_message_id)
                for sp in batch
            ]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for j, result in enumerate(batch_results):
                index = i + j + 1
                if isinstance(result, Exception):
                    result = {'success': False, 'balance': 0, 'message': str(result)}

                if result['success']:
                    results['success'] += 1
                    results['total_transferred'] += result.get('balance', 0)
                else:
                    results['failed'] += 1

                results['details'].append({
                    'session': Path(batch[j]).name,
                    'result': result
                })

                if progress_callback:
                    bal = result.get('balance', 0)
                    status = f"✅ {bal:,}" if result['success'] else f"❌ {result['message'][:25]}"
                    await progress_callback(index, total, f"اکانت {index}/{total}: {status}")

            # تاخیر بین batch‌ها
            if i + workers < total:
                delay = custom_delay if custom_delay is not None else (
                    Config.DELAY_BETWEEN_ACTIONS + random.randint(0, Config.DELAY_RANDOM_RANGE)
                )
                await asyncio.sleep(delay)

        return results

    async def send_mio(self, session_path: str, group_username: str, wait_timeout: int = 15) -> Dict:
        """
        ارسال 'میو' در گروه و دریافت reply ربات
        Returns: {'success': bool, 'message': str, 'points': int}
        """
        client = None
        try:
            session_string = Path(session_path).read_text(encoding='utf-8')
            client = TelegramClient(StringSession(session_string), self.api_id, self.api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                return {'success': False, 'message': 'سشن نامعتبر', 'points': 0}

            try:
                group = await client.get_entity(group_username.lstrip('@'))
            except Exception as e:
                return {'success': False, 'message': f'گروه پیدا نشد: {e}', 'points': 0}

            try:
                sent = await client.send_message(group, 'میو')
            except Exception as e:
                err = str(e)
                if 'banned' in err.lower() or 'UserBannedInChannel' in err:
                    return {'success': False, 'message': 'ban شده', 'points': 0}
                return {'success': False, 'message': f'خطا: {err[:40]}', 'points': 0}

            sent_id = sent.id
            elapsed = 0
            points = 0
            reply_text = ''

            while elapsed < wait_timeout:
                await asyncio.sleep(2)
                elapsed += 2
                msgs = await client.get_messages(group, limit=10, min_id=sent_id)
                for msg in msgs:
                    if msg.id <= sent_id or not msg.text:
                        continue
                    reply_to_id = getattr(getattr(msg, 'reply_to', None), 'reply_to_msg_id', None)
                    if reply_to_id != sent_id:
                        continue
                    # استخراج پوینت از پیام مثلاً "122 میو پوینت گرفتی"
                    m = re.search(r'(\d[\d,،]*)\s*میو پوینت', msg.text)
                    if m:
                        points = int(m.group(1).replace(',', '').replace('،', ''))
                    reply_text = (msg.text or '')[:80]
                    logger.info(f"[mio] {session_path[-20:]} reply: {reply_text}")
                    return {'success': True, 'message': reply_text, 'points': points}

            return {'success': False, 'message': f'timeout {wait_timeout}s', 'points': 0}

        except Exception as e:
            logger.error(f"[mio] خطا: {e}")
            return {'success': False, 'message': str(e)[:60], 'points': 0}
        finally:
            if client:
                await client.disconnect()

    async def send_fish_and_click(self, session_path: str, group_username: str,
                                  button_index: int = 1, wait_timeout: int = 25) -> Dict:
        """
        ارسال 'ماهی'، منتظر edit شدن پیام ربات، کلیک دکمه button_index
        ربات پیشی اول یه پیام می‌فرسته (بدون دکمه)، بعد edit می‌کنه و دکمه اضافه می‌کنه
        Returns: {'success': bool, 'message': str}
        """
        client = None
        try:
            session_string = Path(session_path).read_text(encoding='utf-8')
            client = TelegramClient(StringSession(session_string), self.api_id, self.api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                return {'success': False, 'message': 'سشن نامعتبر'}

            try:
                group = await client.get_entity(group_username.lstrip('@'))
            except Exception as e:
                return {'success': False, 'message': f'گروه پیدا نشد: {e}'}

            try:
                sent = await client.send_message(group, 'ماهی')
            except Exception as e:
                err = str(e)
                if 'banned' in err.lower() or 'UserBannedInChannel' in err:
                    return {'success': False, 'message': 'ban شده'}
                return {'success': False, 'message': f'خطا: {err[:40]}'}

            sent_id = sent.id
            logger.info(f"[fish] {session_path[-20:]} ارسال ماهی msg_id={sent_id}")

            # منتظر reply ربات (اول بدون دکمه، بعد edit با دکمه)
            elapsed = 0
            bot_msg_id = None

            # مرحله ۱: پیدا کردن reply ربات
            while elapsed < wait_timeout and bot_msg_id is None:
                await asyncio.sleep(2)
                elapsed += 2
                msgs = await client.get_messages(group, limit=10, min_id=sent_id)
                for msg in msgs:
                    if msg.id <= sent_id:
                        continue
                    reply_to_id = getattr(getattr(msg, 'reply_to', None), 'reply_to_msg_id', None)
                    if reply_to_id == sent_id:
                        bot_msg_id = msg.id
                        logger.info(f"[fish] پیام ربات پیدا شد id={bot_msg_id}")
                        break

            if bot_msg_id is None:
                return {'success': False, 'message': f'reply ربات نرسید (timeout {wait_timeout}s)'}

            # مرحله ۲: منتظر edit شدن پیام (دکمه اضافه بشه) — حداکثر 65 ثانیه
            edit_elapsed = 0
            edit_timeout = 65

            while edit_elapsed < edit_timeout:
                await asyncio.sleep(3)
                edit_elapsed += 3
                try:
                    fresh = await client.get_messages(group, ids=bot_msg_id)
                    if fresh and fresh.buttons:
                        # دکمه پیدا شد
                        all_buttons = []
                        for row in fresh.buttons:
                            for btn in row:
                                all_buttons.append(btn)

                        if button_index < len(all_buttons):
                            btn = all_buttons[button_index]
                            await btn.click()
                            btn_text = getattr(btn, 'text', '?')
                            logger.info(f"[fish] کلیک دکمه #{button_index}: {btn_text}")
                            return {'success': True, 'message': f'ماهی گرفته شد، دکمه کلیک شد: {btn_text}'}
                        else:
                            return {'success': False, 'message': f'دکمه #{button_index} وجود ندارد ({len(all_buttons)} دکمه)'}
                except Exception as e:
                    logger.warning(f"[fish] خطا در poll edit: {e}")

            return {'success': False, 'message': f'دکمه در {edit_timeout}s ظاهر نشد'}

        except Exception as e:
            logger.error(f"[fish] خطا: {e}")
            return {'success': False, 'message': str(e)[:60]}
        finally:
            if client:
                await client.disconnect()
