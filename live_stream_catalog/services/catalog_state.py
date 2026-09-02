from live_stream_catalog.models import Channel
from live_stream_catalog.services.expiry import needs_refresh

MAX_AGE_BY_SOURCE = {
    "youtube": 1800,
    "kick": 1800,
    "twitch": 1800,
}


def carry_previous_resolution(
    channels: list[Channel],
    current_channels: list[Channel],
) -> list[Channel]:
    current_by_id = {channel.id: channel for channel in current_channels}

    for channel in channels:
        current = current_by_id.get(channel.id)
        if not current or current.source_url != channel.source_url:
            continue

        if current.removed or current.status == "removed":
            channel.status = "removed"
            channel.error = current.error
            channel.stream_url = None
            channel.resolved_at = current.resolved_at
            channel.expires_at = None
            channel.ttl_seconds = None
            channel.removed = True
            channel.removed_at = current.removed_at
            channel.removal_reason = current.removal_reason
            channel.publishable_static = False
            continue

        if not current.stream_url:
            continue

        channel.stream_url = current.stream_url
        channel.status = current.status
        channel.error = current.error
        channel.resolved_at = current.resolved_at
        channel.expires_at = current.expires_at
        channel.ttl_seconds = current.ttl_seconds

    return channels


def split_refresh_candidates(
    channels: list[Channel],
    min_ttl_seconds: int,
) -> tuple[list[Channel], list[Channel]]:
    to_refresh = []
    to_keep = []

    for channel in channels:
        target = to_refresh if needs_refresh(
            channel,
            min_ttl_seconds,
            max_age_by_source=MAX_AGE_BY_SOURCE,
        ) else to_keep
        target.append(channel)

    return to_keep, to_refresh
