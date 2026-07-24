#!/usr/bin/env python3
"""
cache.py — In-Memory Caching with Optional Redis Backend
==========================================================

Provides a simple caching layer for frequently accessed data.
Falls back to in-memory dict if Redis is not available.
"""

import time
import threading
from functools import wraps

# Try to import Redis, fallback to None
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

class Cache:
    """Simple cache with Redis backend or in-memory fallback"""
    
    def __init__(self, redis_url=None, default_ttl=300):
        """
        Initialize cache.
        
        Args:
            redis_url: Redis connection URL (e.g., 'redis://localhost:6379')
            default_ttl: Default time-to-live in seconds (default: 5 minutes)
        """
        self.default_ttl = default_ttl
        self._memory_cache = {}
        self._memory_expiry = {}
        self._lock = threading.Lock()
        
        # Try to connect to Redis
        self._redis = None
        if redis_url and REDIS_AVAILABLE:
            try:
                self._redis = redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                print("[CACHE] Redis connected successfully")
            except Exception as e:
                print(f"[CACHE] Redis connection failed: {e}, using in-memory cache")
                self._redis = None
        else:
            print("[CACHE] Using in-memory cache (Redis not available)")
    
    def get(self, key):
        """Get value from cache"""
        if self._redis:
            try:
                value = self._redis.get(key)
                return value
            except Exception:
                return None
        
        # In-memory fallback
        with self._lock:
            if key in self._memory_cache:
                if self._memory_expiry.get(key, 0) > time.time():
                    return self._memory_cache[key]
                else:
                    # Expired, remove
                    del self._memory_cache[key]
                    del self._memory_expiry[key]
        return None
    
    def set(self, key, value, ttl=None):
        """Set value in cache"""
        if ttl is None:
            ttl = self.default_ttl
        
        if self._redis:
            try:
                self._redis.setex(key, ttl, value)
                return True
            except Exception:
                return False
        
        # In-memory fallback
        with self._lock:
            self._memory_cache[key] = value
            self._memory_expiry[key] = time.time() + ttl
        return True
    
    def delete(self, key):
        """Delete value from cache"""
        if self._redis:
            try:
                self._redis.delete(key)
                return True
            except Exception:
                return False
        
        # In-memory fallback
        with self._lock:
            self._memory_cache.pop(key, None)
            self._memory_expiry.pop(key, None)
        return True
    
    def clear(self, pattern=None):
        """Clear cache (optionally by pattern)"""
        if self._redis:
            try:
                if pattern:
                    keys = self._redis.keys(pattern)
                    if keys:
                        self._redis.delete(*keys)
                else:
                    self._redis.flushdb()
                return True
            except Exception:
                return False
        
        # In-memory fallback
        with self._lock:
            if pattern:
                # Simple pattern matching (supports * at end)
                keys_to_delete = [k for k in self._memory_cache.keys() 
                                 if k.startswith(pattern.replace('*', ''))]
                for key in keys_to_delete:
                    del self._memory_cache[key]
                    del self._memory_expiry[key]
            else:
                self._memory_cache.clear()
                self._memory_expiry.clear()
        return True
    
    def get_or_set(self, key, value_func, ttl=None):
        """Get value from cache, or set it if not exists"""
        value = self.get(key)
        if value is not None:
            return value
        
        value = value_func()
        self.set(key, value, ttl)
        return value

def cached(ttl=300, key_prefix=""):
    """Decorator for caching function results"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Generate cache key from function name and arguments
            cache_key = f"{key_prefix}{func.__name__}:{str(args)}:{str(kwargs)}"
            
            # Try to get from cache
            value = app_cache.get(cache_key)
            if value is not None:
                return value
            
            # Execute function and cache result
            result = func(*args, **kwargs)
            app_cache.set(cache_key, result, ttl)
            return result
        return wrapper
    return decorator

# Global cache instance
app_cache = Cache(
    redis_url="redis://localhost:6379",
    default_ttl=300  # 5 minutes
)
