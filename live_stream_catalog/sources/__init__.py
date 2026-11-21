from live_stream_catalog.sources.registry import get_resource_registry
from live_stream_catalog.services.video_platform_policy import VideoPlatformRequestPolicy


def load_catalog(default_resolution: str = "best", *,
                 video_platform_policy: VideoPlatformRequestPolicy | None = None):
    from live_stream_catalog.sources.loader import load_catalog as _load_catalog

    return _load_catalog(default_resolution=default_resolution,
                         video_platform_policy=video_platform_policy)


__all__ = ["load_catalog", "get_resource_registry"]
