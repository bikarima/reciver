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
    patterns = [
        r'میو پوینت ها?\s*:\s*([\d,]+)',
        r'💰.*?:\s*([\d,]+)',
        r'([\d,]+)\s*🪙',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return int(m.group(1).replace(',', ''))
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
                msgs = await client.get_messages(group, limit=10, min_id=sent_id)

                for msg in msgs:
                    if not msg.text:
                        continue
                    # باید reply به پیام ما باشه
                    is_reply_to_us = (
                        msg.reply_to and
                        msg.reply_to.reply_to_msg_id == sent_id
                    )
                    if not is_reply_to_us:
                        continue

                    parsed = parse_mio_balance(msg.text)
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
            transfer_text = f'انتقال موجودی میویی {balance}'
            await client.send_message(
                group,
                transfer_text,
                reply_to=target_message_id
            )
            logger.info(f"[{session_path}] انتقال {balance} — reply به msg {target_message_id}")

            return {
                'success': True,
                'balance': balance,
                'message': f'انتقال {balance:,} میو پوینت انجام شد'
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
