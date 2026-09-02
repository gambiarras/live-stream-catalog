import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_expiry_result(expire_ts: int) -> tuple[str | None, int | None]:
    expire_dt = datetime.fromtimestamp(expire_ts, tz=timezone.utc)
    ttl_seconds = max(0, int((expire_dt - utc_now()).total_seconds()))
    return expire_dt.isoformat(), ttl_seconds


def _extract_youtube_expiry_from_path(path: str) -> tuple[str | None, int | None]:
    parts = [part for part in path.split("/") if part]

    for index, part in enumerate(parts):
        if part == "expire" and index + 1 < len(parts):
            try:
                expire_ts = int(parts[index + 1])
                return _build_expiry_result(expire_ts)
            except ValueError:
                return None, None

    return None, None


def _extract_expiry_from_query(url: str) -> tuple[str | None, int | None]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    for key in ("expire", "expires", "exp"):
        values = query.get(key)
        if not values:
            continue

        try:
            expire_ts = int(values[0])
            return _build_expiry_result(expire_ts)
        except ValueError:
            continue

    return None, None


def _extract_expiry_from_jwt_query(url: str) -> tuple[str | None, int | None]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    for token in query.get("token", []):
        parts = token.split(".")
        if len(parts) < 2:
            continue

        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            expire_ts = int(data["exp"])
            return _build_expiry_result(expire_ts)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue

    return None, None


def extract_expiry_from_stream_url(url: str | None) -> tuple[str | None, int | None]:
    if not url:
        return None, None

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    if "googlevideo.com" in hostname:
        return _extract_youtube_expiry_from_path(unquote(parsed.path))

    expires_at, ttl_seconds = _extract_expiry_from_query(url)
    if expires_at is not None:
        return expires_at, ttl_seconds

    return _extract_expiry_from_jwt_query(url)


def needs_refresh(channel, min_ttl_seconds: int, max_age_by_source: dict[str, int] | None = None) -> bool:
    if getattr(channel, "removed", False) or channel.status == "removed":
        return False

    if channel.status != "resolved":
        return True

    expires_at = parse_iso_datetime(channel.expires_at)
    if expires_at is not None:
        remaining = int((expires_at - utc_now()).total_seconds())
        return remaining <= min_ttl_seconds

    if max_age_by_source:
        max_age_seconds = max_age_by_source.get(channel.source_type)
        if max_age_seconds is not None:
            resolved_at = parse_iso_datetime(channel.resolved_at)
            if resolved_at is None:
                return True

            age_seconds = int((utc_now() - resolved_at).total_seconds())
            return age_seconds >= max_age_seconds

    return False
