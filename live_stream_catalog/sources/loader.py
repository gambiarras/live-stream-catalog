import json
import logging
from live_stream_catalog.local_config import resource_json

from live_stream_catalog.models import Channel
from live_stream_catalog.services.video_platform_policy import VideoPlatformRequestPolicy
from live_stream_catalog.sources.json_channel_catalog import load_configured_json_catalogs
from live_stream_catalog.sources.registry import get_resource_registry
from live_stream_catalog.sources.script_discovered_catalog import load_configured_rest_catalogs
from live_stream_catalog.sources.addon_catalog import load_configured_addon_catalogs
from live_stream_catalog.sources.directory_catalog import load_configured_directory_catalog
from live_stream_catalog.sources.video_platform_live_discovery import load_video_platform_live_discovery_channels

logger = logging.getLogger(__name__)


def _load_single_resource(resource_name: str, source_type: str, default_resolution: str) -> list[Channel]:
    raw = resource_json(resource_name)

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
        provider = channel.provider_id or channel.source_type
        headers = json.dumps(sorted((str(name).strip().casefold(), str(value).strip())
                                    for name, value in channel.request_headers.items()), separators=(",", ":"))
        if channel.stream_url:
            key = ("stream", provider, channel.stream_url.strip(), headers)
        elif channel.source_url:
            key = ("source", provider, channel.source_url.strip(), headers)
        else:
            key = ("variant", channel.provider_id or channel.source_type, channel.variant_id or channel.id)

        if key in seen_keys:
            logger.warning("Duplicate channel transport skipped id=%s", channel.id)
            continue

        seen_keys.add(key)
        result.append(channel)

    return result


def load_catalog(default_resolution: str = "best", *,
                 video_platform_policy: VideoPlatformRequestPolicy | None = None) -> list[Channel]:
    channels: list[Channel] = []

    channels.extend(
        load_configured_addon_catalogs(
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
        load_video_platform_live_discovery_channels(
            default_resolution=default_resolution,
            continue_on_error=True,
            video_platform_policy=video_platform_policy,
        )
    )
    channels.extend(load_configured_directory_catalog(default_resolution=default_resolution))

    return _deduplicate(channels)
