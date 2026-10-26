from live_stream_catalog.models import Channel
from live_stream_catalog.services.expiry import needs_refresh

MAX_AGE_BY_SOURCE = {
    "youtube": 1800,
    "kick": 1800,
    "twitch": 1800,
}


def _resolution_inputs(channel: Channel) -> tuple[object, ...]:
    """Compare resolution configuration privately, including transport requirements."""
    return (
        channel.logical_channel_id,
        channel.variant_id,
        channel.resolution,
        channel.codec,
        channel.request_headers,
        channel.secret_refs,
        channel.requires_dynamic_resolution,
        channel.publishable_static,
        channel.delivery_mode,
        channel.drm,
    )


def _source_key(channel: Channel) -> tuple[str, str | None, str, str]:
    return channel.id, channel.provider_id, channel.source_type, channel.source_url


def carry_previous_resolution(
    channels: list[Channel],
    current_channels: list[Channel],
) -> list[Channel]:
    """Carry candidates only when discovery still needs the same resolution.

    Discovery transports and failures remain authoritative. Expiry selection
    happens separately in split_refresh_candidates, including carried candidates.
    """
    current_by_source = {_source_key(channel): channel for channel in current_channels}

    for channel in channels:
        if channel.removed or channel.status == "removed":
            continue

        current = current_by_source.get(_source_key(channel))
        if current is None:
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

        if (
            channel.stream_url
            or channel.status != "pending"
            or current.status != "resolved"
            or not current.stream_url
            or (channel.protocol is not None and channel.protocol != current.protocol)
            or _resolution_inputs(channel) != _resolution_inputs(current)
        ):
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
