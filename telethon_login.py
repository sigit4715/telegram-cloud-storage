#!/usr/bin/env python3
"""
Telethon login: kirim OTP + submit code
Usage:
  python3 telethon_login.py send    → kirim OTP, simpan phone_code_hash
  python3 telethon_login.py verify  → baca OTP dari /tmp/telethon_otp + verify
"""
import asyncio, sys, os, json
from telethon import TelegramClient

API_ID = 37930713
API_HASH = '4f0bc1805682a285e40e80e4891e0d85'
PHONE = '+6285789650006'
SESSION = '/root/telegram_cloud_session'
OTP_FILE = '/tmp/telethon_otp'
HASH_FILE = '/tmp/telethon_hash'

async def send_otp():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    sent = await client.send_code_request(PHONE)
    # Simpan phone_code_hash
    with open(HASH_FILE, 'w') as f:
        f.write(sent.phone_code_hash)
    print(f'OTP_SENT: {sent.type} hash={sent.phone_code_hash[:10]}...')
    await client.disconnect()

async def verify():
    code = open(OTP_FILE).read().strip()
    phone_code_hash = open(HASH_FILE).read().strip()
    print(f'Read OTP: {code}, hash: {phone_code_hash[:10]}...')
    
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(PHONE, code, phone_code_hash=phone_code_hash)
    except Exception as e:
        if 'two-step' in str(e).lower() or 'password' in str(e).lower():
            print('NEED_2FA_PASSWORD')
            await client.disconnect()
            return
        raise
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
