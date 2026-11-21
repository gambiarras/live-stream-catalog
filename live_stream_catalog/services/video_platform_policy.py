"""Bounded, private backoff shared by VideoPlatform discovery and resolution."""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from live_stream_catalog.io import write_json_atomic
from live_stream_catalog.services.expiry import utc_now
from live_stream_catalog.local_config import runtime_settings

logger = logging.getLogger(__name__)
INITIAL_DELAY = 3600
MAX_DELAY = 28800
MAX_RETRY_AFTER = 86400


def is_video_platform_rate_limit(exc: Exception) -> bool:
    """Recognize rate limiting without storing the exception or its URLs."""
    message = str(exc).casefold()
    return bool(re.search(r"\b429\b", message)) or any(
        marker in message
        for marker in runtime_settings().get("transient_error_markers", (
            "too many requests",
            "blocked.example.test/sorry",
            "rate limit",
            "rate-limit",
            "ratelimit",
            "temporarily blocked",
        ))
    )


def _retry_after(exc: Exception, now: datetime) -> int:
    """Read Retry-After from bounded exception chains when exposed by clients."""
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        response = getattr(current, "response", None)
        headers = getattr(response, "headers", None) or getattr(
            current, "headers", None
        )
        value = headers.get("Retry-After") if headers else None
        if value is not None:
            try:
                delay = int(value)
            except (TypeError, ValueError):
                try:
                    target = parsedate_to_datetime(str(value))
                    delay = int((target - now).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = 0
            return max(0, min(delay, MAX_RETRY_AFTER))
        current = current.__cause__ or current.__context__
    return 0


@dataclass(slots=True)
class VideoPlatformRequestPolicy:
    """Stop all VideoPlatform work after a block, including across CLI invocations."""

    state_file: Path | None = None
    strikes: int = 0
    blocked_until: datetime | None = None
    rate_limited: bool = False
    successful_resolution: bool = False

    @classmethod
    def for_output(cls, output_path: Path) -> "VideoPlatformRequestPolicy":
        """Load a small private state file outside the public output contract."""
        override = os.environ.get("LIVE_VIDEO_PLATFORM_STATE_FILE")
        path = (
            Path(override)
            if override
            else output_path.parent / ".cache" / "video-platform-rate-limit.json"
        )
        policy = cls(state_file=path)
        try:
            if path.stat().st_size > 4096:
                raise ValueError("Oversized state")
            raw = json.loads(path.read_text(encoding="utf-8"))
            strikes = raw["strikes"]
            until = datetime.fromisoformat(raw["blocked_until"])
            if (
                type(strikes) is not int
                or not 1 <= strikes <= 4
                or until.tzinfo is None
            ):
                raise ValueError("Invalid state")
            policy.strikes = strikes
            policy.blocked_until = min(
                until, utc_now() + timedelta(seconds=MAX_RETRY_AFTER)
            )
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            logger.warning("Ignoring invalid private VideoPlatform backoff state")
        return policy

    def allowed(self) -> bool:
        """Return whether a new discovery or resolver request may begin."""
        return not self.rate_limited and (
            self.blocked_until is None or utc_now() >= self.blocked_until
        )

    def block(self, exc: Exception) -> None:
        """Persist one escalating cooldown per run and honor a bounded Retry-After."""
        if self.rate_limited:
            return
        now = utc_now()
        self.rate_limited = True
        self.strikes = min(4, self.strikes + 1)
        delay = max(
            min(MAX_DELAY, INITIAL_DELAY * 2 ** (self.strikes - 1)),
            _retry_after(exc, now),
        )
        self.blocked_until = now + timedelta(seconds=delay)
        if self.state_file:
            write_json_atomic(
                self.state_file,
                {
                    "version": 1,
                    "strikes": self.strikes,
                    "blocked_until": self.blocked_until.isoformat(),
                },
            )
        logger.warning(
            "VideoPlatform rate limited; pausing discovery/resolution until=%s",
            self.blocked_until.isoformat(),
        )

    def finish(self) -> None:
        """Reset escalation only after a complete run with a real successful resolution."""
        if self.successful_resolution and not self.rate_limited:
            self.strikes = 0
            self.blocked_until = None
            if self.state_file:
                self.state_file.unlink(missing_ok=True)
