"""
Holds the built tree in memory as PRE-SERIALIZED JSON bytes.

This is the key speed trick: serializing a 100k+ node nested structure
is the expensive part of "return a tree as JSON". Do it once per
rebuild, cache the bytes, and every request just hands back a memory
buffer - no per-request tree walk, no per-request orjson.dumps(), no
Pydantic model validation of 100k+ nested objects.
"""
import threading
import time
import logging
from typing import Optional

import orjson

from database import fetch_all_rows
from tree_builder import build_tree

logger = logging.getLogger("tree_cache")


class TreeCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._tree_json: bytes = orjson.dumps([])
        self._nodes_by_id: dict = {}
        self._last_built: float = 0.0
        self._row_count: int = 0

    def rebuild(self) -> float:
        start = time.perf_counter()
        rows = fetch_all_rows()
        roots, nodes = build_tree(rows)
        payload = orjson.dumps(roots)
        with self._lock:
            self._tree_json = payload
            self._nodes_by_id = nodes
            self._last_built = time.time()
            self._row_count = len(rows)
        elapsed = time.perf_counter() - start
        logger.info(f"Tree rebuilt: {self._row_count} rows in {elapsed:.3f}s")
        return elapsed

    def get_full_tree_bytes(self) -> bytes:
        with self._lock:
            return self._tree_json

    def get_subtree_bytes(self, acid: str) -> Optional[bytes]:
        with self._lock:
            node = self._nodes_by_id.get(acid)
            return None if node is None else orjson.dumps(node)

    def get_children_bytes(self, acid: str) -> Optional[bytes]:
        """One level only - for lazy-loading UIs that don't want the
        whole tree in a single response."""
        with self._lock:
            node = self._nodes_by_id.get(acid)
            if node is None:
                return None
            children = [
                {
                    "acid": c["acid"],
                    "acname": c["acname"],
                    "has_children": bool(c["children"]),
                }
                for c in node["children"]
            ]
            return orjson.dumps(children)

    def stats(self) -> dict:
        with self._lock:
            return {"row_count": self._row_count, "last_built": self._last_built}


tree_cache = TreeCache()
