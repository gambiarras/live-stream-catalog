import json
import logging
from importlib import resources

from live_stream_catalog.models import Channel
from live_stream_catalog.sources.json_channel_catalog import load_configured_json_catalogs
from live_stream_catalog.sources.registry import get_resource_registry
from live_stream_catalog.sources.script_discovered_catalog import load_configured_rest_catalogs
from live_stream_catalog.sources.stremio_addon_catalog import load_configured_stremio_addons
from live_stream_catalog.sources.youtube_live_discovery import load_youtube_live_discovery_channels

logger = logging.getLogger(__name__)


def _load_single_resource(resource_name: str, source_type: str, default_resolution: str) -> list[Channel]:
    with resources.files("live_stream_catalog.resources").joinpath(resource_name).open("r", encoding="utf-8") as file:
        raw = json.load(file)

    channels: list[Channel] = []
    for item in raw:
        item = dict(item)
        item["source_type"] = source_type
        channels.append(Channel.from_dict(item, default_resolution=default_resolution))

    return channels


def _deduplicate(channels: list[Channel]) -> list[Channel]:
    seen_keys: set[tuple[str, ...]] = set()
    result: list[Channel] = []

    for channel in channels:
        if channel.stream_url:
            key = ("stream", channel.stream_url.strip())
        elif channel.source_url:
            key = ("source", channel.source_url.strip())
        else:
            key = ("variant", channel.provider_id or channel.source_type, channel.variant_id or channel.id)

        if key in seen_keys:
            logger.warning("Duplicate channel transport skipped id=%s", channel.id)
            continue

        seen_keys.add(key)
        result.append(channel)

    return result


def load_catalog(default_resolution: str = "best") -> list[Channel]:
    channels: list[Channel] = []

    channels.extend(
        load_configured_stremio_addons(
            default_resolution=default_resolution,
            continue_on_error=False,
        )
    )
    channels.extend(
        load_configured_json_catalogs(
            default_resolution=default_resolution,
            continue_on_error=False,
        )
    )
    channels.extend(
        load_configured_rest_catalogs(
            default_resolution=default_resolution,
            continue_on_error=False,
        )
    )

    for resource_name, source_type in get_resource_registry():
        channels.extend(_load_single_resource(resource_name, source_type, default_resolution))

    channels.extend(
        load_youtube_live_discovery_channels(
            default_resolution=default_resolution,
            continue_on_error=True,
        )
    )

    return _deduplicate(channels)
