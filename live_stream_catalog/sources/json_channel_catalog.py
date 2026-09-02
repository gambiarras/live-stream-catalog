import json
import logging
import os
from dataclasses import dataclass
from importlib import resources
from typing import Any

import requests

from live_stream_catalog.models import Channel, SENSITIVE_HEADER_NAMES
from live_stream_catalog.services.expiry import extract_expiry_from_stream_url
from live_stream_catalog.sources.channel_contract import (
    as_bool,
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

SOURCE_TYPE = "json_catalog"
CONFIG_RESOURCE_NAME = "json_channel_catalogs.json"


@dataclass(slots=True, frozen=True)
class JsonChannelCatalogConfig:
    id: str
    provider_id: str
    endpoint_url: str
    request_headers: dict[str, str]
    items_path: str | None = None
    variants_field: str = "variants"
    timeout: int = 30

    @classmethod
    def from_dict(cls, data: dict) -> "JsonChannelCatalogConfig":
        endpoint_url = data.get("endpoint_url")
        endpoint_url_env = data.get("endpoint_url_env")
        if endpoint_url_env:
            endpoint_url = os.environ.get(str(endpoint_url_env), endpoint_url)
        if not endpoint_url:
            raise ValueError(f"Missing JSON catalog endpoint for id={data.get('id')}")

        request_headers: dict[str, str] = {}
        headers_env = data.get("request_headers_env")
        if headers_env and os.environ.get(str(headers_env)):
            raw_headers = json.loads(os.environ[str(headers_env)])
            request_headers = as_string_dict(raw_headers)

        return cls(
            id=str(data["id"]),
            provider_id=str(data.get("provider_id") or data["id"]),
            endpoint_url=str(endpoint_url),
            request_headers=request_headers,
            items_path=data.get("items_path"),
            variants_field=str(data.get("variants_field", "variants")),
            timeout=int(data.get("timeout", 30)),
        )


def _first(item: dict, *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return None


def _nested_value(payload: Any, path: str | None) -> Any:
    if not path:
        return payload
    value = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _catalog_items(payload: Any, items_path: str | None) -> list[dict]:
    value = _nested_value(payload, items_path)
    if items_path is None and isinstance(value, dict):
        for key in ("channels", "items", "data", "results"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list):
        raise RuntimeError("JSON channel catalog items is not a list")
    return [item for item in value if isinstance(item, dict)]


def _variant_items(item: dict, variants_field: str) -> list[dict]:
    variants = item.get(variants_field)
    if not isinstance(variants, list):
        variants = item.get("streams")
    if not isinstance(variants, list) or not variants:
        return [item]
    return [variant for variant in variants if isinstance(variant, dict)]


def _category(item: dict) -> str:
    value = _first(item, "category", "categories", "group", "genre", "genres", "category_name")
    if isinstance(value, dict):
        value = _first(value, "name", "title", "id")
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value or "general")


def _stream_headers(item: dict, variant: dict) -> dict[str, str]:
    return as_string_dict(
        _first(variant, "request_headers", "headers")
        or _first(item, "request_headers", "headers")
    )


def _has_sensitive_headers(headers: dict[str, str]) -> bool:
    return any(name.casefold() in SENSITIVE_HEADER_NAMES for name in headers)


def _channel_from_variant(
    config: JsonChannelCatalogConfig,
    item: dict,
    variant: dict,
    position: int,
    default_resolution: str,
) -> Channel:
    base_id = str(_first(item, "logical_channel_id", "channel_id", "id", "slug", "name"))
    logical_channel_id = f"{config.provider_id}.{slugify(base_id)}"
    base_name = str(_first(item, "name", "title", "display_name") or base_id)
    url_value = _first(variant, "stream_url", "url", "playback_url", "hls_url", "dash_url")
    stream_url = str(url_value) if url_value else None
    resolution = str(_first(variant, "resolution", "quality") or default_resolution)
    codec_value = _first(variant, "codec", "video_codec")
    codec = str(codec_value) if codec_value else None
    variant_label = normalize_variant_label(
        _first(variant, "variant_label", "label", "name", "title", "quality"),
        resolution=resolution,
        codec=codec,
    )
    variant_id = str(
        _first(variant, "variant_id", "id")
        or stable_variant_id(config.provider_id, logical_channel_id, variant_label, position, stream_url)
    )
    removed = as_bool(_first(variant, "removed"), as_bool(_first(item, "removed")))
    drm_value = _first(variant, "drm") or _first(item, "drm")
    drm = drm_value if isinstance(drm_value, dict) else None
    delivery_mode = str(
        _first(variant, "delivery_mode", "deliveryMode")
        or _first(item, "delivery_mode", "deliveryMode")
        or "direct"
    ).casefold()
    dynamic = as_bool(
        _first(variant, "requires_dynamic_resolution"),
        as_bool(_first(item, "requires_dynamic_resolution")),
    )
    headers = _stream_headers(item, variant)
    expires_at, ttl_seconds = extract_expiry_from_stream_url(stream_url)
    default_publishable = bool(
        stream_url
        and not removed
        and not dynamic
        and drm is None
        and delivery_mode == "direct"
        and expires_at is None
        and not _has_sensitive_headers(headers)
    )
    publishable_value = _first(variant, "publishable_static")
    if publishable_value is None:
        publishable_value = _first(item, "publishable_static")
    publishable_static = default_publishable and as_bool(publishable_value, default=True)

    return Channel(
        id=f"{config.id}.{slugify(base_id)}.{slugify(variant_id)}",
        name=display_name_with_variant(base_name, variant_label),
        source_url=f"config://{config.provider_id}/{slugify(base_id)}",
        logo=str(_first(item, "logo", "logo_url", "poster", "image") or ""),
        group=_category(item),
        source_type=SOURCE_TYPE,
        provider_id=config.provider_id,
        logical_channel_id=logical_channel_id,
        variant_id=variant_id,
        variant_label=variant_label,
        tvg_id=_first(item, "tvg_id", "tvgId", "xmltv_id", "epg_id"),
        resolution=resolution,
        codec=codec,
        bitrate=optional_int(_first(variant, "bitrate", "bandwidth")),
        protocol=infer_protocol(stream_url, _first(variant, "protocol", "format")),
        request_headers=headers,
        secret_refs=as_string_dict(
            _first(variant, "secret_refs") or _first(item, "secret_refs")
        ),
        stream_url=stream_url,
        status="removed" if removed else ("resolved" if stream_url else "offline"),
        expires_at=expires_at,
        ttl_seconds=ttl_seconds,
        requires_dynamic_resolution=dynamic,
        publishable_static=publishable_static,
        delivery_mode=delivery_mode,
        drm=drm,
        removed=removed,
        removed_at=_first(variant, "removed_at") or _first(item, "removed_at"),
        removal_reason=_first(variant, "removal_reason") or _first(item, "removal_reason"),
    )


def load_json_catalog_channels(
    configs: list[JsonChannelCatalogConfig],
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
                response = session.get(
                    config.endpoint_url,
                    headers={"Accept": "application/json", **config.request_headers},
                    timeout=config.timeout,
                )
                response.raise_for_status()
                items = _catalog_items(response.json(), config.items_path)
                for item in items:
                    if is_explicit_adult(item):
                        continue
                    for position, variant in enumerate(_variant_items(item, config.variants_field)):
                        if is_explicit_adult(variant):
                            continue
                        channels.append(
                            _channel_from_variant(
                                config,
                                item,
                                variant,
                                position,
                                default_resolution,
                            )
                        )
            except Exception as exc:
                logger.exception("Failed to load JSON channel catalog id=%s error=%s", config.id, exc)
                if not continue_on_error:
                    raise
    finally:
        if owns_session:
            session.close()

    return channels


def load_config_resource() -> list[JsonChannelCatalogConfig]:
    resource = resources.files("live_stream_catalog.resources").joinpath(CONFIG_RESOURCE_NAME)
    if not resource.is_file():
        return []

    configs: list[JsonChannelCatalogConfig] = []
    for item in json.loads(resource.read_text(encoding="utf-8")):
        try:
            configs.append(JsonChannelCatalogConfig.from_dict(item))
        except ValueError as exc:
            logger.info("Configured JSON catalog disabled: %s", exc)
    return configs


def load_configured_json_catalogs(
    default_resolution: str = "best",
    continue_on_error: bool = True,
) -> list[Channel]:
    return load_json_catalog_channels(
        load_config_resource(),
        default_resolution=default_resolution,
        continue_on_error=continue_on_error,
    )
