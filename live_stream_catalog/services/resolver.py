import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from streamlink import Streamlink

from live_stream_catalog.local_config import runtime_settings
from live_stream_catalog.models import Channel
from live_stream_catalog.plugins import register_custom_plugins
from live_stream_catalog.services.expiry import (
    extract_expiry_from_stream_url,
    parse_iso_datetime,
    utc_now,
)
from live_stream_catalog.services.removal import mark_removed, terminal_removal_reason
from live_stream_catalog.services.video_platform_policy import (
    VideoPlatformRequestPolicy,
    is_video_platform_rate_limit,
)
from live_stream_catalog.sources.channel_contract import infer_protocol
from live_stream_catalog.video_platform_transport import is_media_hls_url

logger = logging.getLogger(__name__)

DIRECT_STREAM_SOURCE_TYPES = {"json_catalog", "script_discovered", "addon_catalog", "directory_catalog"}
SERIAL_RESOLVE_SOURCE_TYPES = {"video_platform"}
NO_STREAM_RETRY_SOURCE_TYPES = {"live_platform", "game_platform"}
NO_STREAM_MAX_ATTEMPTS = 3
TRANSIENT_RESOLVE_ERROR_MARKERS = runtime_settings().get("transient_error_markers", (
    "429",
    "too many requests",
    "blocked.example.test/sorry",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "temporarily blocked",
))


def _is_pre_resolved(channel: Channel) -> bool:
    if channel.status != "resolved" or not channel.stream_url:
        return False

    return channel.source_type in DIRECT_STREAM_SOURCE_TYPES or channel.source_url == channel.stream_url


def _is_transient_resolve_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in TRANSIENT_RESOLVE_ERROR_MARKERS)


def _known_transport_expiries(channel: Channel) -> list[datetime]:
    """Keep declared bounds even when a public URL does not encode its expiry."""
    declared = parse_iso_datetime(channel.expires_at)
    if channel.expires_at is not None and declared is None:
        raise ValueError("Invalid declared transport expiry")
    extracted, _ = extract_expiry_from_stream_url(channel.stream_url)
    return [value for value in (declared, parse_iso_datetime(extracted))
                if value is not None]


def _existing_stream_url_has_expired(channel: Channel) -> bool:
    try:
        return any(value.tzinfo is None or value <= utc_now() for value in _known_transport_expiries(channel))
    except ValueError:
        return True


def _preserve_existing_stream_url(channel: Channel, exc: Exception) -> Channel:
    channel.status = "resolved"
    channel.error = f"transient_resolve_error: {exc}"
    expiries = _known_transport_expiries(channel)
    if expiries:
        expiry = min(expiries)
        channel.expires_at = expiry.isoformat()
        channel.ttl_seconds = max(0, int((expiry - utc_now()).total_seconds()))
    channel.protocol = infer_protocol(channel.stream_url, channel.protocol)
    channel.publishable_static = bool(
        channel.publishable_static and not channel.requires_dynamic_resolution
        and channel.expires_at is None and channel.source_type != "video_platform"
    )
    return channel


def _defer_video_platform_channel(channel: Channel) -> Channel:
    """Keep only known unexpired media, without inventing a new successful observation."""
    try:
        expiries = _known_transport_expiries(channel)
        expires_at = min(expiries) if expiries and all(value.tzinfo is not None for value in expiries) else None
    except ValueError:
        expires_at = None
    channel.requires_dynamic_resolution = True
    channel.publishable_static = False
    channel.error = "video_platform_rate_limited"
    if channel.stream_url and expires_at is not None and expires_at > utc_now():
        channel.status = "resolved"
        channel.expires_at = expires_at.isoformat()
        channel.ttl_seconds = int((expires_at - utc_now()).total_seconds())
    else:
        channel.status = "error"
        channel.stream_url = None
        channel.expires_at = None
        channel.ttl_seconds = None
    return channel


def _stream_attempt_count(channel: Channel) -> int:
    if channel.source_type in NO_STREAM_RETRY_SOURCE_TYPES:
        return NO_STREAM_MAX_ATTEMPTS

    return 1


def _resolve_stream(session: Streamlink, channel: Channel):
    stream = None

    for attempt in range(1, _stream_attempt_count(channel) + 1):
        streams = session.streams(channel.source_url)
        stream = streams.get(channel.resolution) or streams.get("best")
        if stream is not None:
            return stream

        if attempt < _stream_attempt_count(channel):
            logger.warning(
                "No stream found, retrying id=%s source_url=%s attempt=%s",
                channel.id,
                channel.source_url,
                attempt,
            )

    return stream


def _stream_expiry_source_url(stream) -> str | None:
    multivariant = getattr(stream, "multivariant", None)
    multivariant_uri = getattr(multivariant, "uri", None)
    return multivariant_uri or getattr(stream, "url", None)


def build_streamlink_session() -> Streamlink:
    session = Streamlink()
    register_custom_plugins(session)
    return session


def resolve_channel(channel: Channel, *,
                    video_platform_policy: VideoPlatformRequestPolicy | None = None) -> Channel:
    channel.declared_inputs()
    if channel.removed or channel.status == "removed":
        channel.removed = True
        channel.publishable_static = False
        return channel

    if (channel.source_type == "video_platform" and video_platform_policy is not None
            and not video_platform_policy.allowed()):
        return _defer_video_platform_channel(channel)

    if channel.source_type == "directory_catalog" and not channel.stream_url:
        # The bounded discovery adapter already inspected this player's declared
        # configuration. An HTML page/unavailable iframe is not a media stream.
        return channel

    if _is_pre_resolved(channel):
        channel.error = None
        if _existing_stream_url_has_expired(channel):
            channel.status = "error"
            channel.error = "expired_transport"
            channel.stream_url = None
            return channel
        expiries = _known_transport_expiries(channel)
        if expiries:
            expiry = min(expiries)
            channel.expires_at = expiry.isoformat()
            channel.ttl_seconds = max(0, int((expiry - utc_now()).total_seconds()))
        if channel.resolved_at is None:
            channel.resolved_at = utc_now().isoformat()
        channel.protocol = infer_protocol(channel.stream_url, channel.protocol)
        if channel.expires_at is not None:
            channel.publishable_static = False
        return channel

    session = build_streamlink_session()

    try:
        stream = _resolve_stream(session, channel)

        if stream is None:
            channel.status = "offline"
            channel.error = "no_stream_found"
            channel.stream_url = None
            channel.expires_at = None
            channel.ttl_seconds = None
            return channel

        channel.stream_url = stream.url
        channel.status = "resolved"
        channel.error = None
        channel.resolved_at = utc_now().isoformat()
        channel.expires_at, channel.ttl_seconds = extract_expiry_from_stream_url(_stream_expiry_source_url(stream))
        channel.protocol = (
            "hls" if channel.source_type == "video_platform" and is_media_hls_url(channel.stream_url)
            else infer_protocol(channel.stream_url, channel.protocol)
        )
        channel.requires_dynamic_resolution = (
            channel.source_type == "video_platform" or channel.source_url != channel.stream_url
        )
        channel.publishable_static = bool(
            channel.publishable_static
            and not channel.requires_dynamic_resolution
            and channel.expires_at is None
        )
        if channel.source_type == "video_platform" and video_platform_policy is not None:
            video_platform_policy.successful_resolution = True
        return channel

    except Exception as exc:
        if (channel.source_type == "video_platform" and video_platform_policy is not None
                and is_video_platform_rate_limit(exc)):
            video_platform_policy.block(exc)
            return _defer_video_platform_channel(channel)
        removal_reason = terminal_removal_reason(
            channel.source_type,
            channel.source_url,
            exc,
        )
        if removal_reason:
            logger.warning(
                "Channel source was permanently removed id=%s reason=%s",
                channel.id,
                removal_reason,
            )
            return mark_removed(channel, removal_reason)

        if channel.stream_url and _is_transient_resolve_error(exc):
            if not _existing_stream_url_has_expired(channel):
                logger.warning(
                    "Keeping previous stream URL after transient resolve error id=%s source_url=%s error=%s",
                    channel.id,
                    channel.source_url,
                    exc,
                )
                return _preserve_existing_stream_url(channel, exc)

            logger.warning(
                "Dropping expired stream URL after transient resolve error id=%s source_url=%s error=%s",
                channel.id,
                channel.source_url,
                exc,
            )

        logger.exception("Failed to resolve channel id=%s source_url=%s", channel.id, channel.source_url)
        channel.status = "error"
        channel.error = str(exc)
        channel.stream_url = None
        channel.expires_at = None
        channel.ttl_seconds = None
        return channel

    finally:
        try:
            session.close()
        except Exception:
            pass


def resolve_channels(channels: list[Channel], max_workers: int, *,
                     video_platform_policy: VideoPlatformRequestPolicy | None = None) -> list[Channel]:
    video_platform_policy = video_platform_policy or VideoPlatformRequestPolicy()
    pre_resolved = [channel for channel in channels if _is_pre_resolved(channel)]
    unresolved = [channel for channel in channels if not _is_pre_resolved(channel)]
    serial_unresolved = [
        channel for channel in unresolved if channel.source_type in SERIAL_RESOLVE_SOURCE_TYPES
    ]
    parallel_unresolved = [
        channel for channel in unresolved if channel.source_type not in SERIAL_RESOLVE_SOURCE_TYPES
    ]
    results = [resolve_channel(channel, video_platform_policy=video_platform_policy) for channel in pre_resolved]

    if parallel_unresolved:
        worker_count = max(1, min(max_workers, len(parallel_unresolved)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(resolve_channel, channel): channel for channel in parallel_unresolved}

            for future in as_completed(futures):
                results.append(future.result())

    results.extend(resolve_channel(channel, video_platform_policy=video_platform_policy) for channel in serial_unresolved)

    return results
