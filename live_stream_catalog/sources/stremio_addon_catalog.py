import json
import logging
import os
from dataclasses import dataclass
from importlib import resources
from urllib.parse import quote, urlencode

import requests

from live_stream_catalog.models import Channel, SENSITIVE_HEADER_NAMES
from live_stream_catalog.services.expiry import extract_expiry_from_stream_url
from live_stream_catalog.sources.channel_contract import (
    as_string_dict,
    display_name_with_variant,
    infer_protocol,
    is_explicit_adult,
    normalize_variant_label,
    optional_int,
    slugify,
    stable_variant_id,
)


logger = logging.getLogger(__name__)

SOURCE_TYPE = "stremio_addon"
CONFIG_RESOURCE_NAME = "stremio_addon_catalogs.json"


@dataclass(slots=True, frozen=True)
class StremioAddonCatalogConfig:
    id: str
    provider_id: str
    manifest_url: str
    types: tuple[str, ...] = ("channel", "tv")
    catalog_ids: tuple[str, ...] = ()
    timeout: int = 30
    max_items: int = 1000
    page_size: int = 100

    @classmethod
    def from_dict(cls, data: dict) -> "StremioAddonCatalogConfig":
        manifest_url = data.get("manifest_url")
        manifest_url_env = data.get("manifest_url_env")
        if manifest_url_env:
            manifest_url = os.environ.get(str(manifest_url_env), manifest_url)
        if not manifest_url:
            raise ValueError(f"Missing addon manifest URL for id={data.get('id')}")

        return cls(
            id=str(data["id"]),
            provider_id=str(data.get("provider_id") or data["id"]),
            manifest_url=str(manifest_url),
            types=tuple(str(item) for item in data.get("types", ("channel", "tv"))),
            catalog_ids=tuple(str(item) for item in data.get("catalog_ids", ())),
            timeout=int(data.get("timeout", 30)),
            max_items=int(data.get("max_items", 1000)),
            page_size=int(data.get("page_size", 100)),
        )


def _request_json(session: requests.Session, url: str, timeout: int) -> dict:
    response = session.get(
        url,
        headers={"Accept": "application/json", "User-Agent": "live-stream-catalog/0.1"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Addon response is not a JSON object: {url}")
    return payload


def _addon_base_url(manifest_url: str) -> str:
    suffix = "/manifest.json"
    return manifest_url[:-len(suffix)] if manifest_url.endswith(suffix) else manifest_url.rstrip("/")


def resource_url(
    manifest_url: str,
    resource_name: str,
    item_type: str,
    item_id: str,
    extra: dict[str, str] | None = None,
) -> str:
    path = (
        f"{_addon_base_url(manifest_url)}/{quote(resource_name, safe='')}"
        f"/{quote(item_type, safe='')}/{quote(item_id, safe='')}"
    )
    if extra:
        path = f"{path}/{urlencode(extra)}"
    return f"{path}.json"


def _catalog_is_selected(catalog: dict, config: StremioAddonCatalogConfig) -> bool:
    catalog_type = str(catalog.get("type") or "")
    catalog_id = str(catalog.get("id") or "")
    if catalog_type not in config.types or not catalog_id:
        return False
    return not config.catalog_ids or catalog_id in config.catalog_ids


def _catalog_supports_skip(catalog: dict) -> bool:
    extra = catalog.get("extra")
    if not isinstance(extra, list):
        return False
    return any(isinstance(item, dict) and item.get("name") == "skip" for item in extra)


def _load_catalog_metas(
    session: requests.Session,
    config: StremioAddonCatalogConfig,
    catalog: dict,
) -> list[dict]:
    catalog_type = str(catalog["type"])
    catalog_id = str(catalog["id"])
    supports_skip = _catalog_supports_skip(catalog)
    metas: list[dict] = []
    skip = 0

    while len(metas) < config.max_items:
        extra = {"skip": str(skip)} if supports_skip and skip else None
        payload = _request_json(
            session,
            resource_url(config.manifest_url, "catalog", catalog_type, catalog_id, extra),
            config.timeout,
        )
        page = payload.get("metas", [])
        if not isinstance(page, list):
            raise RuntimeError(f"Addon catalog metas is not a list: {catalog_id}")

        page_items = [item for item in page if isinstance(item, dict)]
        metas.extend(page_items[: max(0, config.max_items - len(metas))])
        if not supports_skip or not page_items or len(page_items) < config.page_size:
            break
        skip += len(page_items)

    return metas


def _meta_category(meta: dict, catalog: dict) -> str:
    genres = meta.get("genres")
    if isinstance(genres, list) and genres:
        return str(genres[0])
    return str(meta.get("category") or catalog.get("name") or "general")


def _stream_headers(stream: dict) -> dict[str, str]:
    behavior_hints = stream.get("behaviorHints")
    if not isinstance(behavior_hints, dict):
        return as_string_dict(stream.get("request_headers") or stream.get("headers"))

    proxy_headers = behavior_hints.get("proxyHeaders")
    if isinstance(proxy_headers, dict):
        return as_string_dict(proxy_headers.get("request"))
    return as_string_dict(stream.get("request_headers") or stream.get("headers"))


def _stream_drm(stream: dict) -> dict | None:
    for value in (stream.get("drm"), stream.get("widevine")):
        if isinstance(value, dict):
            return value
    behavior_hints = stream.get("behaviorHints")
    if isinstance(behavior_hints, dict) and isinstance(behavior_hints.get("drm"), dict):
        return behavior_hints["drm"]
    return None


def _delivery_mode(stream: dict) -> str:
    behavior_hints = stream.get("behaviorHints")
    explicit = stream.get("delivery_mode") or stream.get("deliveryMode")
    if not explicit and isinstance(behavior_hints, dict):
        explicit = behavior_hints.get("deliveryMode")
    return str(explicit or "direct").casefold()


def _stream_is_not_web_ready(stream: dict) -> bool:
    behavior_hints = stream.get("behaviorHints")
    return isinstance(behavior_hints, dict) and behavior_hints.get("notWebReady") is True


def _has_sensitive_headers(headers: dict[str, str]) -> bool:
    return any(name.casefold() in SENSITIVE_HEADER_NAMES for name in headers)


def _stream_url(stream: dict) -> str | None:
    value = stream.get("url") or stream.get("stream_url")
    return str(value) if value else None


def _stream_source_url(stream: dict, fallback_url: str) -> str:
    if stream.get("ytId"):
        return f"https://www.youtube.com/watch?v={stream['ytId']}"
    return str(stream.get("externalUrl") or _stream_url(stream) or fallback_url)


def _meta_video_ids(meta: dict) -> list[str]:
    videos = meta.get("videos")
    if isinstance(videos, list):
        ids = [str(video.get("id")) for video in videos if isinstance(video, dict) and video.get("id")]
        if ids:
            return ids
    return [str(meta["id"])] if meta.get("id") else []


def _channels_from_streams(
    config: StremioAddonCatalogConfig,
    catalog: dict,
    meta: dict,
    video_id: str,
    streams: list[dict],
    default_resolution: str,
) -> list[Channel]:
    logical_id = str(meta.get("logical_channel_id") or meta.get("tvg_id") or meta.get("id") or video_id)
    public_source_ref = f"config://{config.provider_id}/{slugify(logical_id)}"
    base_name = str(meta.get("name") or logical_id)
    result: list[Channel] = []

    for position, stream in enumerate(streams):
        url = _stream_url(stream)
        if not url and not stream.get("ytId"):
            continue

        resolution = str(stream.get("resolution") or stream.get("quality") or default_resolution)
        codec = stream.get("codec")
        variant_label = normalize_variant_label(
            stream.get("name"),
            stream.get("title"),
            stream.get("description"),
            stream.get("quality"),
            resolution=resolution,
            codec=str(codec) if codec else None,
        )
        variant_id = stable_variant_id(
            config.provider_id,
            logical_id,
            variant_label,
            position,
            url or str(stream.get("ytId")),
        )
        headers = _stream_headers(stream)
        drm = _stream_drm(stream)
        delivery_mode = _delivery_mode(stream)
        dynamic = bool(stream.get("ytId")) or _stream_is_not_web_ready(stream)
        expires_at, ttl_seconds = extract_expiry_from_stream_url(url)
        publishable_static = bool(
            url
            and not dynamic
            and drm is None
            and delivery_mode == "direct"
            and expires_at is None
            and not _has_sensitive_headers(headers)
        )

        result.append(
            Channel(
                id=variant_id,
                name=display_name_with_variant(base_name, variant_label),
                source_url=_stream_source_url(stream, public_source_ref),
                logo=str(meta.get("poster") or meta.get("logo") or ""),
                group=_meta_category(meta, catalog),
                source_type=SOURCE_TYPE,
                provider_id=config.provider_id,
                logical_channel_id=f"{config.provider_id}.{slugify(logical_id)}",
                variant_id=variant_id,
                variant_label=variant_label,
                tvg_id=meta.get("tvg_id") or meta.get("tvgId"),
                resolution=resolution,
                codec=str(codec) if codec else None,
                bitrate=optional_int(stream.get("bitrate")),
                protocol=infer_protocol(url, stream.get("protocol")),
                request_headers=headers,
                secret_refs=as_string_dict(stream.get("secret_refs")),
                stream_url=url,
                status="resolved" if url else "pending",
                expires_at=expires_at,
                ttl_seconds=ttl_seconds,
                requires_dynamic_resolution=dynamic,
                publishable_static=publishable_static,
                delivery_mode=delivery_mode,
                drm=drm,
            )
        )

    return result


def load_stremio_addon_channels(
    configs: list[StremioAddonCatalogConfig],
    default_resolution: str = "best",
    session: requests.Session | None = None,
    continue_on_error: bool = True,
) -> list[Channel]:
    owns_session = session is None
    session = session or requests.Session()
    channels: list[Channel] = []

    try:
        for config in configs:
            try:
                manifest = _request_json(session, config.manifest_url, config.timeout)
                if is_explicit_adult(manifest.get("behaviorHints", {})):
                    logger.warning("Explicitly adult addon skipped id=%s", config.id)
                    continue

                catalogs = manifest.get("catalogs", [])
                if not isinstance(catalogs, list):
                    raise RuntimeError("Addon manifest catalogs is not a list")

                for catalog in catalogs:
                    if not isinstance(catalog, dict) or not _catalog_is_selected(catalog, config):
                        continue
                    for meta in _load_catalog_metas(session, config, catalog):
                        if is_explicit_adult(meta):
                            continue
                        for video_id in _meta_video_ids(meta):
                            endpoint = resource_url(
                                config.manifest_url,
                                "stream",
                                str(meta.get("type") or catalog["type"]),
                                video_id,
                            )
                            payload = _request_json(session, endpoint, config.timeout)
                            streams = payload.get("streams", [])
                            if not isinstance(streams, list):
                                continue
                            channels.extend(
                                _channels_from_streams(
                                    config,
                                    catalog,
                                    meta,
                                    video_id,
                                    [item for item in streams if isinstance(item, dict)],
                                    default_resolution,
                                )
                            )
            except Exception as exc:
                logger.exception("Failed to load addon catalog id=%s error=%s", config.id, exc)
                if not continue_on_error:
                    raise
    finally:
        if owns_session:
            session.close()

    return channels


def load_config_resource() -> list[StremioAddonCatalogConfig]:
    resource = resources.files("live_stream_catalog.resources").joinpath(CONFIG_RESOURCE_NAME)
    if not resource.is_file():
        return []

    configs: list[StremioAddonCatalogConfig] = []
    for item in json.loads(resource.read_text(encoding="utf-8")):
        try:
            configs.append(StremioAddonCatalogConfig.from_dict(item))
        except ValueError as exc:
            logger.info("Configured addon catalog disabled: %s", exc)
    return configs


def load_configured_stremio_addons(
    default_resolution: str = "best",
    continue_on_error: bool = True,
) -> list[Channel]:
    return load_stremio_addon_channels(
        load_config_resource(),
        default_resolution=default_resolution,
        continue_on_error=continue_on_error,
    )
