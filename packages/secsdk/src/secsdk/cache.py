"""A tiny file cache for public, slow-changing responses (KEV catalog, SEC tickers...).

Only used when a request passes ``cache_ttl``. Authenticated requests should never
be cached; providers are responsible for not passing a TTL on those.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


class FileCache:
    def __init__(self, directory: Path, enabled: bool = True) -> None:
        self.directory = Path(directory)
        self.enabled = enabled

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.directory / digest[:2] / f"{digest}.json"

    def get(self, key: str, ttl: float) -> tuple[int, str] | None:
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            if time.time() - path.stat().st_mtime > ttl:
                return None
            entry = json.loads(path.read_text())
            return int(entry["status"]), str(entry["body"])
        except (OSError, ValueError, KeyError):
            return None

    def set(self, key: str, status: int, body: str) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"status": status, "body": body}))
            tmp.replace(path)
        except OSError:
            pass  # caching is best-effort

    def clear(self) -> int:
        removed = 0
        if self.directory.exists():
            for p in self.directory.rglob("*.json"):
                p.unlink(missing_ok=True)
                removed += 1
        return removed
