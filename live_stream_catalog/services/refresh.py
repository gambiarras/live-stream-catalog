import logging

from live_stream_catalog.config import AppConfig
from live_stream_catalog.local_config import require_local_configuration
from live_stream_catalog.models import Channel
from live_stream_catalog.services.catalog_pipeline import (
    finish_catalog_run,
    prepare_catalog_run,
)
from live_stream_catalog.services.discovery import current_discovery, discovery_scope
from live_stream_catalog.services.resolver import resolve_channels
from live_stream_catalog.services.video_platform_policy import (
    VideoPlatformRequestPolicy,
)
from live_stream_catalog.sources.addon_catalog import (
    SOURCE_TYPE as ADDON_SOURCE_TYPE,
)
from live_stream_catalog.sources.addon_catalog import (
    load_configured_addon_catalogs,
)
from live_stream_catalog.sources.json_channel_catalog import (
    SOURCE_TYPE as JSON_CATALOG_SOURCE_TYPE,
)
from live_stream_catalog.sources.json_channel_catalog import (
    load_configured_json_catalogs,
)
from live_stream_catalog.sources.script_discovered_catalog import (
    SCRIPT_DISCOVERED_SOURCE_TYPE,
    load_configured_rest_catalogs,
)
from live_stream_catalog.sources.video_platform_live_discovery import (
    SOURCE_TYPE as VIDEO_PLATFORM_SOURCE_TYPE,
)
from live_stream_catalog.sources.video_platform_live_discovery import (
    load_video_platform_live_discovery_channels,
)

logger = logging.getLogger(__name__)

DYNAMIC_SOURCE_TYPES = {
    JSON_CATALOG_SOURCE_TYPE,
    SCRIPT_DISCOVERED_SOURCE_TYPE,
    ADDON_SOURCE_TYPE,
    VIDEO_PLATFORM_SOURCE_TYPE,
}


def _current_channels_by_source(
    current_channels: list[Channel],
    source_type: str,
) -> list[Channel]:
    values = [channel for channel in current_channels if channel.source_type == source_type]
    context = current_discovery()
    if context:
        context.retain(values, reason="discovery_failed")
    return values



def _load_dynamic_channels(
    config: AppConfig,
    current_channels: list[Channel],
    video_platform_policy: VideoPlatformRequestPolicy | None = None,
) -> list[Channel]:
    dynamic_channels: list[Channel] = []

    try:
        dynamic_channels.extend(
            load_configured_addon_catalogs(
                default_resolution=config.default_resolution,
                continue_on_error=False,
            )
        )
    except Exception as exc:
        logger.exception(
            "Failed to refresh configured addon catalogs, keeping existing channels error=%s",
            exc,
        )
        dynamic_channels.extend(
            _current_channels_by_source(current_channels, ADDON_SOURCE_TYPE)
        )

    try:
        dynamic_channels.extend(
            load_configured_json_catalogs(
                default_resolution=config.default_resolution,
                continue_on_error=False,
            )
        )
    except Exception as exc:
        logger.exception(
            "Failed to refresh configured JSON catalogs, keeping existing channels error=%s",
            exc,
        )
        dynamic_channels.extend(
            _current_channels_by_source(current_channels, JSON_CATALOG_SOURCE_TYPE)
        )

    if video_platform_policy is not None and not video_platform_policy.allowed():
        logger.info("Video platform discovery deferred during rate-limit cooldown; retaining existing identities")
        retained = _current_channels_by_source(current_channels, VIDEO_PLATFORM_SOURCE_TYPE)
        context = current_discovery()
        if context:
            if retained:
                context.retain(retained, reason="rate_limited", origin="deferred")
            else:
                context.observe(VIDEO_PLATFORM_SOURCE_TYPE, VIDEO_PLATFORM_SOURCE_TYPE, [],
                                origin="deferred", complete=False, reason="rate_limited")
        dynamic_channels.extend(retained)
    else:
        try:
            dynamic_channels.extend(
                load_video_platform_live_discovery_channels(
                    default_resolution=config.default_resolution,
                    continue_on_error=False,
                    video_platform_policy=video_platform_policy,
                )
            )
        except Exception as exc:
            logger.error("Video platform discovery failed; keeping existing channels error_type=%s",
                         type(exc).__name__)
            dynamic_channels.extend(_current_channels_by_source(current_channels, VIDEO_PLATFORM_SOURCE_TYPE))

    try:
        dynamic_channels.extend(
            load_configured_rest_catalogs(
                default_resolution=config.default_resolution,
                continue_on_error=False,
            )
        )
    except Exception as exc:
        logger.exception(
            "Failed to refresh script-discovered catalogs, keeping existing channels error=%s",
            exc,
        )
        dynamic_channels.extend(_current_channels_by_source(current_channels, SCRIPT_DISCOVERED_SOURCE_TYPE))

    return dynamic_channels


def run_refresh(config: AppConfig) -> None:
    logger.info("Starting partial refresh")
    require_local_configuration()

    run = prepare_catalog_run(config)
    current_channels = run.previous.channels
    if not current_channels:
        logger.info("No existing catalog found, falling back to full build")
        from live_stream_catalog.services.build import run_build
        run_build(config)
        return
    with discovery_scope(run.discovery):
        dynamic_channels = _load_dynamic_channels(
            config, current_channels, video_platform_policy=run.video_platform_policy)
        static_channels = [channel for channel in current_channels
                           if channel.source_type not in DYNAMIC_SOURCE_TYPES]
        run.discovery.retain(static_channels, reason="not_due_in_refresh", origin="cache")
        metadata = finish_catalog_run(run, static_channels + dynamic_channels, resolver=resolve_channels)

    logger.info(
        "Refresh finished total=%s resolved=%s offline=%s removed=%s errors=%s",
        metadata.total_channels,
        metadata.resolved_channels,
        metadata.offline_channels,
        metadata.removed_channels,
        metadata.error_channels,
    )
