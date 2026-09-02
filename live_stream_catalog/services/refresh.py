import logging

from live_stream_catalog.config import AppConfig
from live_stream_catalog.io import load_channels_file, write_json_atomic
from live_stream_catalog.models import Channel
from live_stream_catalog.services.catalog_state import carry_previous_resolution, split_refresh_candidates
from live_stream_catalog.services.metadata import build_run_metadata
from live_stream_catalog.services.resolver import resolve_channels
from live_stream_catalog.sources.json_channel_catalog import (
    SOURCE_TYPE as JSON_CATALOG_SOURCE_TYPE,
    load_configured_json_catalogs,
)
from live_stream_catalog.sources.script_discovered_catalog import (
    SCRIPT_DISCOVERED_SOURCE_TYPE,
    load_configured_rest_catalogs,
)
from live_stream_catalog.sources.stremio_addon_catalog import (
    SOURCE_TYPE as STREMIO_ADDON_SOURCE_TYPE,
    load_configured_stremio_addons,
)
from live_stream_catalog.sources.youtube_live_discovery import (
    SOURCE_TYPE as YOUTUBE_SOURCE_TYPE,
    load_youtube_live_discovery_channels,
)

logger = logging.getLogger(__name__)

DYNAMIC_SOURCE_TYPES = {
    JSON_CATALOG_SOURCE_TYPE,
    SCRIPT_DISCOVERED_SOURCE_TYPE,
    STREMIO_ADDON_SOURCE_TYPE,
    YOUTUBE_SOURCE_TYPE,
}


def _current_channels_by_source(
    current_channels: list[Channel],
    source_type: str,
) -> list[Channel]:
    return [
        channel
        for channel in current_channels
        if channel.source_type == source_type
    ]



def _load_dynamic_channels(
    config: AppConfig,
    current_channels: list[Channel],
) -> list[Channel]:
    dynamic_channels: list[Channel] = []

    try:
        dynamic_channels.extend(
            load_configured_stremio_addons(
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
            _current_channels_by_source(current_channels, STREMIO_ADDON_SOURCE_TYPE)
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

    try:
        dynamic_channels.extend(
            load_youtube_live_discovery_channels(
                default_resolution=config.default_resolution,
                continue_on_error=False,
            )
        )
    except Exception as exc:
        logger.exception(
            "Failed to refresh YouTube live discovery, keeping existing channels error=%s",
            exc,
        )
        dynamic_channels.extend(_current_channels_by_source(current_channels, YOUTUBE_SOURCE_TYPE))

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

    current_channels = load_channels_file(
        config.output_path,
        default_resolution=config.default_resolution,
    )

    if not current_channels:
        logger.info("No existing catalog found, falling back to full build")
        from live_stream_catalog.services.build import run_build
        run_build(config)
        return

    dynamic_channels = carry_previous_resolution(
        _load_dynamic_channels(config, current_channels),
        current_channels,
    )
    static_channels = [
        channel
        for channel in current_channels
        if channel.source_type not in DYNAMIC_SOURCE_TYPES
    ]

    to_keep, to_refresh = split_refresh_candidates(static_channels + dynamic_channels, config.min_ttl_seconds)

    logger.info(
        "Refresh selection total=%s refresh=%s keep=%s",
        len(current_channels),
        len(to_refresh),
        len(to_keep),
    )

    refreshed = resolve_channels(to_refresh, max_workers=config.max_workers)

    merged = to_keep + refreshed
    merged.sort(key=lambda item: item.id.casefold())

    write_json_atomic(config.output_path, [channel.to_dict() for channel in merged])

    metadata = build_run_metadata(merged)
    write_json_atomic(config.meta_output_path, metadata.to_dict())

    logger.info(
        "Refresh finished total=%s resolved=%s offline=%s removed=%s errors=%s",
        metadata.total_channels,
        metadata.resolved_channels,
        metadata.offline_channels,
        metadata.removed_channels,
        metadata.error_channels,
    )
