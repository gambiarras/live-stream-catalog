from urllib.parse import urlparse

from live_stream_catalog.models import Channel
from live_stream_catalog.services.expiry import utc_now


REMOVABLE_SOURCE_TYPES = {"youtube", "twitch", "kick"}
TERMINAL_REMOVAL_MARKERS = {
    "account_not_found": (
        "account does not exist",
        "account not found",
        "no such user",
        "this account has been terminated",
        "this account is no longer available",
    ),
    "channel_not_found": (
        "channel does not exist",
        "channel not found",
        "this channel does not exist",
    ),
    "user_not_found": (
        "user does not exist",
        "user not found",
        "this user does not exist",
    ),
}


def _is_account_or_channel_url(source_type: str, source_url: str) -> bool:
    parsed = urlparse(source_url)
    path = parsed.path.casefold()

    if source_type == "youtube":
        if path == "/watch" or path.startswith("/shorts/"):
            return False
        return any(marker in path for marker in ("/@", "/channel/", "/c/", "/user/", "/live", "/streams"))

    return source_type in {"twitch", "kick"} and bool(path.strip("/"))


def terminal_removal_reason(
    source_type: str,
    source_url: str,
    error: Exception | str,
) -> str | None:
    if source_type not in REMOVABLE_SOURCE_TYPES:
        return None
    if not _is_account_or_channel_url(source_type, source_url):
        return None

    message = str(error).casefold()
    for reason, markers in TERMINAL_REMOVAL_MARKERS.items():
        if any(marker in message for marker in markers):
            return reason
    return None


def mark_removed(channel: Channel, reason: str) -> Channel:
    channel.status = "removed"
    channel.removed = True
    channel.removed_at = utc_now().isoformat()
    channel.removal_reason = reason
    channel.error = reason
    channel.stream_url = None
    channel.resolved_at = channel.removed_at
    channel.expires_at = None
    channel.ttl_seconds = None
    channel.publishable_static = False
    return channel
