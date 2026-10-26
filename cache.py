"""Redis cache for the complete account list used to build report trees."""
import logging
import os
import threading
from decimal import Decimal
from typing import Any, Dict, List

import orjson
import redis

from database import fetch_account_data_version, fetch_account_rows

logger = logging.getLogger("account_cache")
CACHE_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_IDLE_SECONDS = int(os.getenv("TREE_CACHE_IDLE_SECONDS", "60"))
CACHE_KEY_PREFIX = os.getenv("TREE_CACHE_KEY_PREFIX", "trial-balance:tree:")
ACCOUNT_LIST_KEY = f"{CACHE_KEY_PREFIX}rmd-aclist"


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Type {type(value).__name__} is not JSON serializable")


class RedisAccountCache:
    def __init__(
        self,
        redis_url: str = CACHE_REDIS_URL,
        idle_seconds: int = CACHE_IDLE_SECONDS,
        key: str = ACCOUNT_LIST_KEY,
    ):
        self.client = redis.Redis.from_url(
            redis_url,
            decode_responses=False,
            protocol=2,
        )
        self.idle_seconds = idle_seconds
        self.key = key
        self._lock = threading.RLock()

    def get_account_rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            cached = self.client.get(self.key)
            source_version = fetch_account_data_version()
            if cached is not None:
                cached_value = orjson.loads(cached)
                if (
                    isinstance(cached_value, dict)
                    and cached_value.get("source_version") == source_version
                    and isinstance(cached_value.get("rows"), list)
                ):
                    self.client.expire(self.key, self.idle_seconds)
                    logger.info(
                        "RMD_ACLIST cache hit: source_version=%s",
                        source_version,
                    )
                    return cached_value["rows"]

            for _ in range(3):
                account_rows = fetch_account_rows()
                refreshed_version = fetch_account_data_version()
                if refreshed_version == source_version:
                    payload = orjson.dumps(
                        {
                            "source_version": refreshed_version,
                            "rows": account_rows,
                        },
                        default=_json_default,
                    )
                    self.client.setex(self.key, self.idle_seconds, payload)
                    logger.info(
                        "RMD_ACLIST cache refreshed: source_version=%s rows=%d",
                        refreshed_version,
                        len(account_rows),
                    )
                    return account_rows
                source_version = refreshed_version

            raise RuntimeError(
                "Account source tables changed repeatedly while refreshing "
                "the RMD_ACLIST cache."
            )

    def stats(self) -> Dict[str, Any]:
        return {
            "cache_backend": "redis",
            "cached_data": "rmd_aclist",
            "cache_idle_expiry_seconds": self.idle_seconds,
        }


account_cache = RedisAccountCache()
