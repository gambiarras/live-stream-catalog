from live_stream_catalog.sources.registry import get_resource_registry


def load_catalog(default_resolution: str = "best"):
    from live_stream_catalog.sources.loader import load_catalog as _load_catalog

    return _load_catalog(default_resolution=default_resolution)


__all__ = ["load_catalog", "get_resource_registry"]
