import logging
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

from live_stream_catalog.local_config import (
    discovery_client_factory,
    resource_json,
    source_settings,
)
from live_stream_catalog.models import Channel
from live_stream_catalog.models.channel import normalize_tags
from live_stream_catalog.services.discovery import (
    configuration_fingerprint,
    current_discovery,
    observe_source,
)
from live_stream_catalog.services.expiry import utc_now
from live_stream_catalog.services.removal import mark_removed, terminal_removal_reason
from live_stream_catalog.services.video_platform_policy import (
    VideoPlatformRequestPolicy,
    is_video_platform_rate_limit,
)

logger = logging.getLogger(__name__)


CONFIG_RESOURCE_NAME = "video_platform_live_discovery_channels.json"
SOURCE_TYPE = "video_platform"
VIDEO_PLATFORM_WATCH_URL = source_settings("video_platform").get("watch_url", "https://video.example.test/watch?v={video_id}")
VIDEO_PLATFORM_BASE_URL = source_settings("video_platform").get("base_url", "https://video.example.test/")


@dataclass(slots=True, frozen=True)
class VideoPlatformLiveDiscoveryConfig:
    id: str
    name: str
    logo: str
    group: str
    tvg_id: str | None = None
    handle: str | None = None
    streams_url: str | None = None
    url: str | None = None
    fallback_url: str | None = None
    use_fallback_when_empty: bool = True
    max_results: int = 50
    include_upcoming: bool = False
    resolution: str | None = None
    feed_id: str | None = None
    tags: tuple[str, ...] = ()
    cache_ttl_seconds: int = 900
    cache_lkg_seconds: int = 21600

    def __post_init__(self) -> None:
        if self.cache_ttl_seconds < 0 or self.cache_lkg_seconds < self.cache_ttl_seconds:
            raise ValueError("Invalid discovery cache lifetime")

    @classmethod
    def from_dict(cls, data: dict) -> "VideoPlatformLiveDiscoveryConfig":
        return cls(
            id=data["id"],
            name=data["name"],
            logo=data.get("logo", ""),
            group=data.get("group", "general"),
            tvg_id=data.get("tvg_id"),
            handle=data.get("handle"),
            streams_url=data.get("streams_url"),
            url=data.get("url"),
            fallback_url=data.get("fallback_url"),
            use_fallback_when_empty=bool(data.get("use_fallback_when_empty", True)),
            max_results=int(data.get("max_results", 50)),
            include_upcoming=bool(data.get("include_upcoming", False)),
            resolution=data.get("resolution"),
            feed_id=data.get("feed_id"), tags=tuple(normalize_tags(data.get("tags"))),
            cache_ttl_seconds=int(data.get("cache_ttl_seconds", 900)),
            cache_lkg_seconds=int(data.get("cache_lkg_seconds", 21600)),
        )


def load_config_resource() -> list[VideoPlatformLiveDiscoveryConfig]:
    return [
        VideoPlatformLiveDiscoveryConfig.from_dict(item)
        for item in resource_json(CONFIG_RESOURCE_NAME)
    ]


def _streams_url(config: VideoPlatformLiveDiscoveryConfig) -> str:
    if config.streams_url:
        return config.streams_url.rstrip("/")

    if config.url:
        url = config.url.rstrip("/")
        if url.endswith("/streams"):
            return url
        if url.endswith("/live"):
            return f"{url[:-5]}/streams"
        return f"{url}/streams"

    if not config.handle:
        raise ValueError(f"VideoPlatform live discovery config requires handle or url: {config.id}")

    handle = config.handle if config.handle.startswith("@") else f"@{config.handle}"
    return f"{VIDEO_PLATFORM_BASE_URL}{handle}/streams"


def _fallback_url(config: VideoPlatformLiveDiscoveryConfig) -> str:
    if config.fallback_url:
        return config.fallback_url

    if config.streams_url:
        url = config.streams_url.rstrip("/")
        if url.endswith("/streams"):
            return f"{url[:-8]}/live"
        return f"{url}/live"

    if config.url:
        url = config.url.rstrip("/")
        if url.endswith("/streams"):
            return f"{url[:-8]}/live"
        return config.url

    handle = config.handle if config.handle and config.handle.startswith("@") else f"@{config.handle}"
    return f"{VIDEO_PLATFORM_BASE_URL}{handle}/live"


def _entry_video_id(entry: dict) -> str | None:
    video_id = entry.get("id")
    if video_id:
        return str(video_id)

    url = entry.get("url")
    if not url:
        return None

    parsed = urlparse(str(url))
    if parsed.netloc in source_settings("video_platform").get("page_hosts", ["video.example.test"]) and parsed.path == "/watch":
        from urllib.parse import parse_qs

        return parse_qs(parsed.query).get("v", [None])[0]

    if parsed.netloc in source_settings("video_platform").get("short_hosts", ["video-short.example.test"]):
        return parsed.path.strip("/") or None

    return None


def _entry_is_usable(entry: dict, include_upcoming: bool) -> bool:
    live_status = entry.get("live_status")
    if live_status == "is_live":
        return True

    if include_upcoming and live_status == "is_upcoming":
        return True

    return entry.get("is_live") is True


def _entry_title(config: VideoPlatformLiveDiscoveryConfig, entry: dict) -> str:
    title = entry.get("title")
    if title:
        return f"{config.name} - {title}"
    return config.name


def _entry_logo(config: VideoPlatformLiveDiscoveryConfig, entry: dict) -> str:
    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list) and thumbnails:
        thumbnail = thumbnails[-1]
        if isinstance(thumbnail, dict) and thumbnail.get("url"):
            return str(thumbnail["url"])

    return config.logo


def _video_platform_dl_options(max_results: int) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": max_results,
        # Retry in later scheduled runs, rather than repeating requests into a block.
        "extractor_retries": 0,
        "retries": 0,
    }

    cookie_file = os.environ.get("VIDEO_PLATFORM_COOKIES_FILE") or os.environ.get("DISCOVERY_COOKIES_FILE")
    if cookie_file:
        options["cookiefile"] = cookie_file

    return options


def _fallback_channel(config: VideoPlatformLiveDiscoveryConfig) -> Channel:
    return Channel(
        id=config.id,
        name=config.name,
        source_url=_fallback_url(config),
        logo=config.logo,
        group=config.group,
        source_type=SOURCE_TYPE,
        provider_id=SOURCE_TYPE,
        logical_channel_id=config.id,
        variant_id=config.id,
        tvg_id=config.tvg_id,
        feed_id=config.feed_id, tags=list(config.tags),
        resolution=config.resolution or "best",
        requires_dynamic_resolution=True,
        publishable_static=False,
    )


def discover_live_channels(
    config: VideoPlatformLiveDiscoveryConfig,
    video_platform_dl_cls=None,
) -> list[Channel]:
    if video_platform_dl_cls is None:
        video_platform_dl_cls = discovery_client_factory()

    streams_url = _streams_url(config)
    with video_platform_dl_cls(_video_platform_dl_options(config.max_results)) as ydl:
        payload = ydl.extract_info(streams_url, download=False)

    if not isinstance(payload, dict) or "entries" not in payload:
        raise ValueError("Invalid discovery inventory")
    entries = payload["entries"]
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes, dict)):
        raise TypeError("Invalid discovery inventory entries")
    if not entries:
        return [_fallback_channel(config)] if config.use_fallback_when_empty else []

    channels: list[Channel] = []
    seen_video_ids: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict) or not _entry_is_usable(entry, config.include_upcoming):
            continue

        video_id = _entry_video_id(entry)
        if not video_id or video_id in seen_video_ids:
            continue

        seen_video_ids.add(video_id)
        source_url = VIDEO_PLATFORM_WATCH_URL.format(video_id=video_id)
        channels.append(
            Channel(
                id=f"{config.id}.{video_id}",
                name=_entry_title(config, entry),
                source_url=source_url,
                logo=_entry_logo(config, entry),
                group=config.group,
                source_type=SOURCE_TYPE,
                provider_id=SOURCE_TYPE,
                logical_channel_id=config.id,
                variant_id=f"{config.id}.{video_id}",
                tvg_id=config.tvg_id,
                feed_id=config.feed_id,
                tags=normalize_tags([*config.tags, *normalize_tags(entry.get("tags"))]),
                resolution=config.resolution or "best",
                requires_dynamic_resolution=True,
                publishable_static=False,
            )
        )

    if not channels and config.use_fallback_when_empty:
        return [_fallback_channel(config)]

    return channels


def load_video_platform_live_discovery_channels(
    default_resolution: str = "best",
    video_platform_dl_cls=None,
    configs: list[VideoPlatformLiveDiscoveryConfig] | None = None,
    continue_on_error: bool = True,
    video_platform_policy: VideoPlatformRequestPolicy | None = None,
) -> list[Channel]:
    channels: list[Channel] = []
    video_platform_policy = video_platform_policy or VideoPlatformRequestPolicy()

    for config in configs if configs is not None else load_config_resource():
        config = replace(config, resolution=config.resolution or default_resolution)
        instance = SOURCE_TYPE + ":" + config.id
        context = current_discovery()
        cookie_file = os.environ.get("VIDEO_PLATFORM_COOKIES_FILE") or os.environ.get("DISCOVERY_COOKIES_FILE")
        cookie_context = None
        if cookie_file:
            try:
                stat = Path(cookie_file).stat()
                cookie_context = (cookie_file, stat.st_mtime_ns, stat.st_size)
            except OSError:
                cookie_context = (cookie_file, None, None)
        fingerprint = configuration_fingerprint({"configuration": asdict(config),
                                                "platform": source_settings(SOURCE_TYPE),
                                                "cookie_context": cookie_context})
        cached = (context.cache.read(instance, fingerprint, max_age_seconds=config.cache_lkg_seconds)
                  if context else None)

        if not video_platform_policy.allowed():
            values = cached.channels if cached is not None else [_fallback_channel(config)]
            if cached is None:
                values[0].error = "video_platform_rate_limited"
            observe_source(instance, SOURCE_TYPE, values, origin="deferred", complete=False,
                           reason="rate_limited", observed_at=cached.observed_at if cached else None)
            channels.extend(values)
            continue
        if cached is not None and config.cache_ttl_seconds and cached.age_seconds <= config.cache_ttl_seconds:
            observe_source(instance, SOURCE_TYPE, cached.channels, origin="cache", reason="within_ttl",
                           observed_at=cached.observed_at)
            channels.extend(cached.channels)
            continue

        try:
            values = discover_live_channels(config, video_platform_dl_cls=video_platform_dl_cls)
            observed_at = utc_now().isoformat()
            if context:
                context.cache.write(instance, fingerprint, values, observed_at)
            observe_source(instance, SOURCE_TYPE, values, reason="discovered" if values else "empty",
                           observed_at=observed_at)
            channels.extend(values)
        except Exception as exc:
            if is_video_platform_rate_limit(exc):
                video_platform_policy.block(exc)
            removal_reason = terminal_removal_reason(
                SOURCE_TYPE,
                _streams_url(config),
                exc,
            )
            if removal_reason:
                values = [mark_removed(_fallback_channel(config), removal_reason)]
                observed_at = utc_now().isoformat()
                if context:
                    context.cache.write(instance, fingerprint, values, observed_at)
                observe_source(instance, SOURCE_TYPE, values, reason="removed", observed_at=observed_at)
                channels.extend(values)
                continue
            if cached is not None:
                origin = "deferred" if not video_platform_policy.allowed() else "retained"
                reason = "rate_limited" if origin == "deferred" else "discovery_error"
                observe_source(instance, SOURCE_TYPE, cached.channels, origin=origin, complete=False,
                               reason=reason, observed_at=cached.observed_at)
                channels.extend(cached.channels)
                continue
            values = [_fallback_channel(config)]
            observe_source(instance, SOURCE_TYPE, values, complete=False, reason="discovery_error")
            channels.extend(values)
            logger.error(
                "Failed to discover VideoPlatform live streams id=%s error_type=%s",
                config.id,
                type(exc).__name__,
            )
            if not continue_on_error:
                raise

    return channels
