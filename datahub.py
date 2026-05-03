"""Lightweight in-process data hub for futures-panel.

Inspired by FinceptTerminal's DataHub design, but implemented as a small
Python utility for this Flask app:
- topic-based cache
- per-topic TTL / min refresh interval
- in-flight de-duplication
- safe fetch wrapper for flaky data providers such as AKShare

No external broker, no threads unless caller uses them. This file is internal
infrastructure and intentionally small.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

try:
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover - pandas exists in app runtime
    pd = None


@dataclass
class HubEntry:
    value: Any
    ts: float
    ttl: float
    source: str = ""
    error: Optional[str] = None


class DataHub:
    """Tiny topic cache with pull-through fetch support."""

    def __init__(self) -> None:
        self._data: dict[str, HubEntry] = {}
        self._inflight: set[str] = set()
        self._last_request: dict[str, float] = {}
        self._lock = threading.RLock()

    def publish(self, topic: str, value: Any, ttl: float = 60, source: str = "") -> Any:
        with self._lock:
            self._data[topic] = HubEntry(value=value, ts=time.time(), ttl=ttl, source=source)
        return value

    def peek(self, topic: str, allow_stale: bool = False) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(topic)
            if not entry:
                return None
            if allow_stale or (time.time() - entry.ts <= entry.ttl):
                return entry.value
            return None

    def meta(self, topic: str) -> dict[str, Any]:
        with self._lock:
            entry = self._data.get(topic)
            if not entry:
                return {"hit": False}
            age = time.time() - entry.ts
            return {
                "hit": True,
                "age": round(age, 3),
                "fresh": age <= entry.ttl,
                "ttl": entry.ttl,
                "source": entry.source,
                "error": entry.error,
            }

    def request(
        self,
        topic: str,
        fetcher: Callable[[], Any],
        ttl: float = 60,
        min_interval: float = 1,
        force: bool = False,
        source: str = "",
    ) -> Any:
        """Return cached topic or synchronously fetch once.

        `min_interval` protects upstreams from repeated misses/rage clicks.
        If a topic is in-flight, returns stale value if available; otherwise None.
        """
        now = time.time()
        with self._lock:
            entry = self._data.get(topic)
            if entry and not force and now - entry.ts <= entry.ttl:
                return entry.value
            if topic in self._inflight:
                return entry.value if entry else None
            last = self._last_request.get(topic, 0)
            if not force and now - last < min_interval:
                return entry.value if entry else None
            self._inflight.add(topic)
            self._last_request[topic] = now

        try:
            value = fetcher()
            self.publish(topic, value, ttl=ttl, source=source)
            return value
        except Exception as e:
            with self._lock:
                entry = self._data.get(topic)
                if entry:
                    entry.error = str(e)
                    return entry.value
            raise
        finally:
            with self._lock:
                self._inflight.discard(topic)


def _jsonable(value: Any) -> Any:
    """Convert common provider return objects into JSON-safe Python objects."""
    if pd is not None:
        if isinstance(value, pd.DataFrame):
            df = value.copy()
            df = df.where(pd.notna(df), None)
            return df.to_dict(orient="records")
        if isinstance(value, pd.Series):
            s = value.where(pd.notna(value), None)
            return s.to_dict()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def safe_ak_call(
    func: Callable[..., Any],
    *args: Any,
    retries: int = 2,
    delay: float = 0.6,
    jsonable: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fault-tolerant provider call wrapper.

    Returns a consistent envelope instead of letting random AKShare/network
    exceptions leak into API handlers.
    """
    last_error: Optional[str] = None
    started = time.time()
    for attempt in range(1, retries + 1):
        try:
            data = func(*args, **kwargs)
            empty = False
            if pd is not None and isinstance(data, pd.DataFrame):
                empty = data.empty
            elif data is None:
                empty = True
            return {
                "ok": True,
                "data": _jsonable(data) if jsonable else data,
                "empty": empty,
                "attempt": attempt,
                "elapsed_ms": round((time.time() - started) * 1000),
                "error": None,
            }
        except Exception as e:  # noqa: BLE001 - provider boundary
            last_error = str(e)
            if attempt < retries:
                time.sleep(delay * attempt)
    return {
        "ok": False,
        "data": None,
        "empty": True,
        "attempt": retries,
        "elapsed_ms": round((time.time() - started) * 1000),
        "error": last_error,
    }


hub = DataHub()
