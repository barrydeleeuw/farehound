"""SerpAPI response cache — records live responses and replays them offline.

Usage:
    # In production (live API calls, caches responses):
    client = SerpAPIClient(api_key="...", cache_dir="data/serpapi_cache")

    # In testing (replay only, no API calls):
    client = SerpAPIClient(api_key="dummy", cache_dir="data/serpapi_cache", offline=True)
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ResponseCache:
    """Cache SerpAPI responses to disk for replay."""

    def __init__(self, cache_dir: str | Path) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def key(self, params: dict) -> str:
        clean = {k: v for k, v in sorted(params.items()) if k != "api_key"}
        return hashlib.md5(json.dumps(clean).encode()).hexdigest()

    def get(self, params: dict) -> dict | None:
        path = self._dir / f"{self.key(params)}.json"
        if path.exists():
            logger.debug("Cache HIT: %s", self.key(params))
            return json.loads(path.read_text())
        return None

    def put(self, params: dict, data: dict) -> None:
        path = self._dir / f"{self.key(params)}.json"
        path.write_text(json.dumps(data, indent=2))
        logger.debug("Cache PUT: %s", self.key(params))

    @property
    def count(self) -> int:
        return len(list(self._dir.glob("*.json")))
