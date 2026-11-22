"""Bounded per-instance discovery snapshots and explicit freshness observations."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from live_stream_catalog.io.json_store import write_json_atomic
from live_stream_catalog.models import Channel
from live_stream_catalog.services.expiry import utc_now

logger = logging.getLogger(__name__)
CACHE_VERSION = 1
MAX_CACHE_FILES = 128
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CACHE_AGE = 86_400
MAX_CACHE_CHANNELS = 1000


def configuration_fingerprint(configuration: dict[str, Any]) -> str:
    """Use a private configuration identity, never a public configuration/secret hash."""
    return hashlib.sha256(
        json.dumps(
            configuration, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


@dataclass(slots=True)
class CachedDiscovery:
    channels: list[Channel]
    observed_at: str
    age_seconds: float
    complete: bool = True


@dataclass(slots=True)
class DiscoveryCache:
    root: Path

    def path(self, instance: str) -> Path:
        return self.root / (hashlib.sha256(instance.encode()).hexdigest() + ".json")

    def prune(self) -> None:
        if not self.root.is_dir():
            return
        keep = []
        for path in self.root.glob("*.json"):
            if len(path.stem) != 64 or any(
                char not in "0123456789abcdef" for char in path.stem
            ):
                continue
            if path.is_symlink():
                path.unlink()
                continue
            stat = path.stat()
            age = utc_now().timestamp() - stat.st_mtime
            if stat.st_size > MAX_FILE_BYTES or age > MAX_CACHE_AGE:
                path.unlink()
            else:
                keep.append((stat.st_mtime_ns, stat.st_size, path))
        total = 0
        for index, (_, size, path) in enumerate(sorted(keep, reverse=True)):
            if index >= MAX_CACHE_FILES or total + size > MAX_CACHE_BYTES:
                path.unlink()
            else:
                total += size

    def read(
        self, instance: str, fingerprint: str, *, max_age_seconds: int
    ) -> CachedDiscovery | None:
        path = self.path(instance)
        try:
            if (
                not path.exists()
                or path.is_symlink()
                or path.stat().st_size > MAX_FILE_BYTES
            ):
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("version") != CACHE_VERSION
                or payload.get("fingerprint") != fingerprint
            ):
                return None
            observed = datetime.fromisoformat(payload["observed_at"])
            if observed.tzinfo is None:
                return None
            age = (utc_now() - observed).total_seconds()
            if not 0 <= age <= min(max_age_seconds, MAX_CACHE_AGE):
                return None
            rows = payload["channels"]
            if (
                not isinstance(rows, list)
                or len(rows) > MAX_CACHE_CHANNELS
                or payload.get("complete") is not True
            ):
                return None
            channels = [Channel.from_state_dict(item) for item in rows]
            return CachedDiscovery(channels, observed.isoformat(), age)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            logger.warning("Ignoring invalid private discovery snapshot")
            return None

    def write(
        self, instance: str, fingerprint: str, channels: list[Channel], observed_at: str
    ) -> None:
        payload = {
            "version": CACHE_VERSION,
            "fingerprint": fingerprint,
            "observed_at": observed_at,
            "complete": True,
            "channels": [channel.to_state_dict() for channel in channels],
        }
        if (
            len(channels) > MAX_CACHE_CHANNELS
            or len(json.dumps(payload).encode()) > MAX_FILE_BYTES
        ):
            logger.warning("Discovery snapshot exceeds its private cache limit")
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json_atomic(self.path(instance), payload)
            self.path(instance).chmod(0o600)
            self.prune()
        except OSError:
            logger.warning("Could not persist private discovery snapshot")


@dataclass(slots=True)
class DiscoveryContext:
    cache: DiscoveryCache
    previous_observations: dict[str, Any] = field(default_factory=dict)
    observations: dict[str, Any] = field(default_factory=dict)

    def observe(
        self,
        instance: str,
        source_type: str,
        channels: list[Channel],
        *,
        origin: str,
        complete: bool,
        reason: str,
        observed_at: str | None = None,
    ) -> None:
        if origin not in {"fresh", "cache", "retained", "deferred"}:
            raise ValueError("Unknown discovery origin")
        if origin == "fresh" and complete and observed_at is None:
            observed_at = utc_now().isoformat()
        elif origin != "fresh" and observed_at is None:
            previous = self.observations.get(
                instance
            ) or self.previous_observations.get(instance, {})
            observed_at = previous.get("observed_at")
        self.observations[instance] = {
            "source_type": source_type,
            "origin": origin,
            "complete": complete,
            "reason": reason,
            "channels": len(channels),
            "observed_at": observed_at,
        }
        for channel in channels:
            channel._source_instance = instance

    def retain(
        self, channels: list[Channel], *, reason: str, origin: str = "retained"
    ) -> None:
        grouped: dict[str, list[Channel]] = {}
        for channel in channels:
            grouped.setdefault(
                channel._source_instance or channel.source_type, []
            ).append(channel)
        for instance, items in grouped.items():
            previous = self.previous_observations.get(instance, {})
            self.observe(
                instance,
                items[0].source_type,
                items,
                origin=origin,
                complete=False,
                reason=reason,
                observed_at=previous.get("observed_at"),
            )

    def complete_unrecorded(self, channels: list[Channel]) -> None:
        grouped: dict[str, list[Channel]] = {}
        for channel in channels:
            grouped.setdefault(
                channel._source_instance or channel.provider_id or channel.source_type,
                [],
            ).append(channel)
        for instance, items in grouped.items():
            if instance not in self.observations:
                self.observe(
                    instance,
                    items[0].source_type,
                    items,
                    origin="fresh",
                    complete=True,
                    reason="discovered",
                )


_ACTIVE: ContextVar[DiscoveryContext | None] = ContextVar(
    "catalog_discovery_context", default=None
)


def current_discovery() -> DiscoveryContext | None:
    return _ACTIVE.get()


@contextmanager
def discovery_scope(context: DiscoveryContext) -> Iterator[DiscoveryContext]:
    token = _ACTIVE.set(context)
    try:
        try:
            context.cache.prune()
        except OSError:
            logger.warning("Could not prune private discovery snapshots")
        yield context
    finally:
        _ACTIVE.reset(token)


def observe_source(
    instance: str,
    source_type: str,
    channels: list[Channel],
    *,
    origin: str = "fresh",
    complete: bool = True,
    reason: str = "discovered",
    observed_at: str | None = None,
) -> None:
    context = current_discovery()
    if context:
        context.observe(
            instance,
            source_type,
            channels,
            origin=origin,
            complete=complete,
            reason=reason,
            observed_at=observed_at,
        )
