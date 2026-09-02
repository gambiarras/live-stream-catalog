import logging

from live_stream_catalog.config import AppConfig
from live_stream_catalog.io import load_channels_file, write_json_atomic
from live_stream_catalog.services.catalog_state import carry_previous_resolution, split_refresh_candidates
from live_stream_catalog.services.metadata import build_run_metadata
from live_stream_catalog.services.resolver import resolve_channels
from live_stream_catalog.sources import load_catalog

logger = logging.getLogger(__name__)

SOURCE_PRIORITY = {
    "stremio_addon": 50,
    "json_catalog": 40,
    "script_discovered": 35,
    "youtube": 30,
    "twitch": 20,
    "kick": 25,
    "unknown": 10,
}


def _sort_for_resolution(channels):
    return sorted(channels, key=lambda item: SOURCE_PRIORITY.get(item.source_type, 10))


def run_build(config: AppConfig) -> None:
    logger.info("Starting full build")

    current_channels = load_channels_file(
        config.output_path,
        default_resolution=config.default_resolution,
    )
    catalog = load_catalog(default_resolution=config.default_resolution)
    catalog = carry_previous_resolution(catalog, current_channels)

    to_keep, to_refresh = split_refresh_candidates(catalog, config.min_ttl_seconds)
    to_refresh = _sort_for_resolution(to_refresh)

    logger.info(
        "Build selection total=%s refresh=%s keep=%s",
        len(catalog),
        len(to_refresh),
        len(to_keep),
    )

    resolved = to_keep + resolve_channels(to_refresh, max_workers=config.max_workers)
    resolved.sort(key=lambda item: item.id.casefold())

    write_json_atomic(config.output_path, [channel.to_dict() for channel in resolved])

    metadata = build_run_metadata(resolved)
    write_json_atomic(config.meta_output_path, metadata.to_dict())

    logger.info(
        "Build finished total=%s resolved=%s offline=%s removed=%s errors=%s",
        metadata.total_channels,
        metadata.resolved_channels,
        metadata.offline_channels,
        metadata.removed_channels,
        metadata.error_channels,
    )
