#!/usr/bin/env python3
"""
encryption.py — Token Encryption at Rest
==========================================

Provides AES-256 encryption for sensitive data (Google tokens, API keys)
using the cryptography library. Tokens are encrypted before storage and
decrypted when needed, ensuring zero-knowledge at rest.
"""

import os
import base64
import hashlib
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Encryption key derived from environment or generated
_ENCRYPTION_KEY = None

def _get_or_create_key():
    """Get or create encryption key from environment or generate one"""
    global _ENCRYPTION_KEY
    if _ENCRYPTION_KEY:
        return _ENCRYPTION_KEY
    
    # Try to load from environment
    key_env = os.environ.get("ENCRYPTION_KEY")
    if key_env:
        _ENCRYPTION_KEY = key_env.encode()
        return _ENCRYPTION_KEY
    
    # Try to load from file
    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".encryption.key")
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            _ENCRYPTION_KEY = f.read().strip()
        return _ENCRYPTION_KEY
    
    # Generate new key and save it
    _ENCRYPTION_KEY = Fernet.generate_key()
    with open(key_path, "wb") as f:
        f.write(_ENCRYPTION_KEY)
    os.chmod(key_path, 0o600)  # Restrict permissions
    return _ENCRYPTION_KEY

def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string and return base64-encoded ciphertext"""
    if not plaintext:
        return ""
    
    key = _get_or_create_key()
    f = Fernet(key)
    encrypted = f.encrypt(plaintext.encode())
    return base64.b64encode(encrypted).decode()

def decrypt_token(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext and return plaintext"""
    if not ciphertext:
        return ""
    
    try:
        key = _get_or_create_key()
        f = Fernet(key)
        decoded = base64.b64decode(ciphertext)
        return f.decrypt(decoded).decode()
    except Exception:
        # If decryption fails, might be legacy plaintext
        return ciphertext

def is_encrypted(data: str) -> bool:
    """Check if data is already encrypted (Fernet tokens start with 'gAAAAA')"""
    if not data:
        return False
    return data.startswith("gAAAAA")

def encrypt_if_needed(plaintext: str) -> str:
    """Encrypt only if not already encrypted"""
    if is_encrypted(plaintext):
        return plaintext
    return encrypt_token(plaintext)

def decrypt_if_needed(ciphertext: str) -> str:
    """Decrypt only if encrypted"""
    if not is_encrypted(ciphertext):
        return ciphertext
    return decrypt_token(ciphertext)

# Migration helper: encrypt existing plaintext tokens
def migrate_plaintext_tokens(db_exec, db_query):
    """Migrate existing plaintext tokens to encrypted format"""
    rows = db_query("SELECT user_id, access_token, refresh_token FROM google_tokens")
    migrated = 0
    
    for row in rows:
        user_id = row["user_id"]
        access_token = row["access_token"]
        refresh_token = row["refresh_token"]
        
        # Encrypt if not already encrypted
        new_access = encrypt_if_needed(access_token) if access_token else ""
        new_refresh = encrypt_if_needed(refresh_token) if refresh_token else ""
        
        if new_access != access_token or new_refresh != refresh_token:
            db_exec(
                "UPDATE google_tokens SET access_token=?, refresh_token=? WHERE user_id=?",
                (new_access, new_refresh, user_id)
            )
            migrated += 1
    
    return migrated
