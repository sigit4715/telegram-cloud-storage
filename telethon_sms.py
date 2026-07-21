#!/usr/bin/env python3
"""
Telethon login via SMS
Usage:
  python3 telethon_sms.py send    → kirim OTP via SMS
  python3 telethon_sms.py verify  → baca OTP dari /tmp/telethon_otp
"""
import asyncio, sys
from telethon import TelegramClient
from telethon.tl.types import AuthSentCode

API_ID = 37930713
API_HASH = '4f0bc1805682a285e40e80e4891e0d85'
PHONE = '+6285789650006'
SESSION = '/root/telegram_cloud_session'
OTP_FILE = '/tmp/telethon_otp'
HASH_FILE = '/tmp/telethon_hash'

async def send_otp():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    # force SMS
    sent = await client.send_code_request(PHONE, force_sms=True)
    with open(HASH_FILE, 'w') as f:
        f.write(sent.phone_code_hash)
    code_type = type(sent).__name__
    print(f'OTP_SENT via {code_type} hash={sent.phone_code_hash[:10]}...')
    await client.disconnect()

async def verify():
    code = open(OTP_FILE).read().strip()
    phone_code_hash = open(HASH_FILE).read().strip()
    print(f'Verifying code: {code}')
    
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(PHONE, code, phone_code_hash=phone_code_hash)
    except Exception as e:
        if 'two-step' in str(e).lower() or 'password' in str(e).lower():
            print('NEED_2FA_PASSWORD')
            await client.disconnect()
            return
        print(f'ERROR: {e}')
        await client.disconnect()
        return
    me = await client.get_me()
    print(f'LOGIN_OK: {me.username or me.first_name} id={me.id}')
    await client.disconnect()

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'send':
        asyncio.run(send_otp())
    elif cmd == 'verify':
        asyncio.run(verify())
    else:
        print(__doc__)
