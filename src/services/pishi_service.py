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
            sent = await client.send_message(group, 'میوهام')
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
