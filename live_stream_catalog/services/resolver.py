import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from streamlink import Streamlink

from live_stream_catalog.models import Channel
from live_stream_catalog.plugins import register_custom_plugins
from live_stream_catalog.services.expiry import (
    extract_expiry_from_stream_url,
    parse_iso_datetime,
    utc_now,
)
from live_stream_catalog.services.removal import mark_removed, terminal_removal_reason
from live_stream_catalog.sources.channel_contract import infer_protocol

logger = logging.getLogger(__name__)

DIRECT_STREAM_SOURCE_TYPES = {"json_catalog", "script_discovered", "stremio_addon"}
SERIAL_RESOLVE_SOURCE_TYPES = {"youtube"}
NO_STREAM_RETRY_SOURCE_TYPES = {"kick", "twitch"}
NO_STREAM_MAX_ATTEMPTS = 3
TRANSIENT_RESOLVE_ERROR_MARKERS = (
    "429",
    "too many requests",
    "google.com/sorry",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "temporarily blocked",
)


def _is_pre_resolved(channel: Channel) -> bool:
    if channel.status != "resolved" or not channel.stream_url:
        return False

    return channel.source_type in DIRECT_STREAM_SOURCE_TYPES or channel.source_url == channel.stream_url


def _is_transient_resolve_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in TRANSIENT_RESOLVE_ERROR_MARKERS)


def _existing_stream_url_has_expired(channel: Channel) -> bool:
    expires_at = parse_iso_datetime(channel.expires_at)
    if expires_at is None:
        extracted_expires_at, _ttl_seconds = extract_expiry_from_stream_url(channel.stream_url)
        expires_at = parse_iso_datetime(extracted_expires_at)

    return expires_at is not None and expires_at <= utc_now()


def _preserve_existing_stream_url(channel: Channel, exc: Exception) -> Channel:
    channel.status = "resolved"
    channel.error = f"transient_resolve_error: {exc}"
    channel.resolved_at = utc_now().isoformat()
    channel.expires_at, channel.ttl_seconds = extract_expiry_from_stream_url(channel.stream_url)
    channel.protocol = infer_protocol(channel.stream_url, channel.protocol)
    channel.publishable_static = channel.expires_at is None
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


def resolve_channel(channel: Channel) -> Channel:
    if channel.removed or channel.status == "removed":
        channel.removed = True
        channel.publishable_static = False
        return channel

    if _is_pre_resolved(channel):
        channel.error = None
        channel.resolved_at = utc_now().isoformat()
        channel.expires_at, channel.ttl_seconds = extract_expiry_from_stream_url(channel.stream_url)
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
            channel.resolved_at = utc_now().isoformat()
            channel.expires_at = None
            channel.ttl_seconds = None
            return channel

        channel.stream_url = stream.url
        channel.status = "resolved"
        channel.error = None
        channel.resolved_at = utc_now().isoformat()
        channel.expires_at, channel.ttl_seconds = extract_expiry_from_stream_url(_stream_expiry_source_url(stream))
        channel.protocol = infer_protocol(channel.stream_url, channel.protocol)
        channel.requires_dynamic_resolution = channel.source_url != channel.stream_url
        channel.publishable_static = bool(
            channel.publishable_static
            and not channel.requires_dynamic_resolution
            and channel.expires_at is None
        )
        return channel

    except Exception as exc:
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
        channel.resolved_at = utc_now().isoformat()
        channel.expires_at = None
        channel.ttl_seconds = None
        return channel

    finally:
        try:
            session.close()
        except Exception:
            pass


def resolve_channels(channels: list[Channel], max_workers: int) -> list[Channel]:
    pre_resolved = [channel for channel in channels if _is_pre_resolved(channel)]
    unresolved = [channel for channel in channels if not _is_pre_resolved(channel)]
    serial_unresolved = [
        channel for channel in unresolved if channel.source_type in SERIAL_RESOLVE_SOURCE_TYPES
    ]
    parallel_unresolved = [
        channel for channel in unresolved if channel.source_type not in SERIAL_RESOLVE_SOURCE_TYPES
    ]
    results = [resolve_channel(channel) for channel in pre_resolved]

    if parallel_unresolved:
        worker_count = max(1, min(max_workers, len(parallel_unresolved)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(resolve_channel, channel): channel for channel in parallel_unresolved}

            for future in as_completed(futures):
                results.append(future.result())

    results.extend(resolve_channel(channel) for channel in serial_unresolved)

    return results
