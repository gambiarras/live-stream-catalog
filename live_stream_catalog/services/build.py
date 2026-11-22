import logging

from live_stream_catalog.config import AppConfig
from live_stream_catalog.local_config import require_local_configuration
from live_stream_catalog.services.catalog_pipeline import (
    finish_catalog_run,
    prepare_catalog_run,
)
from live_stream_catalog.services.discovery import discovery_scope
from live_stream_catalog.services.resolver import resolve_channels
from live_stream_catalog.sources import load_catalog

logger = logging.getLogger(__name__)

SOURCE_PRIORITY = {
    "addon_catalog": 50,
    "json_catalog": 40,
    "script_discovered": 35,
    "video_platform": 30,
    "game_platform": 20,
    "live_platform": 25,
    "unknown": 10,
}


def _sort_for_resolution(channels):
    return sorted(channels, key=lambda item: SOURCE_PRIORITY.get(item.source_type, 10))


def run_build(config: AppConfig) -> None:
    logger.info("Starting full build")
    require_local_configuration()

    run = prepare_catalog_run(config)
    with discovery_scope(run.discovery):
        catalog = load_catalog(default_resolution=config.default_resolution,
                               video_platform_policy=run.video_platform_policy)
        metadata = finish_catalog_run(run, catalog, sort_for_resolution=_sort_for_resolution,
                                      resolver=resolve_channels)

    logger.info(
        "Build finished total=%s resolved=%s offline=%s removed=%s errors=%s",
        metadata.total_channels,
        metadata.resolved_channels,
        metadata.offline_channels,
        metadata.removed_channels,
        metadata.error_channels,
    )
