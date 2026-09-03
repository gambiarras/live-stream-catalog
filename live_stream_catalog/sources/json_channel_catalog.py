import json
import logging
import os
from dataclasses import dataclass, field
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
    linked_entries_url: str | None = None
    linked_ids_path: str | None = None
    linked_query_param: str = "id"
    linked_query_params: dict[str, str] = field(default_factory=dict)
    item_id_path: str | None = None
    item_name_path: str | None = None
    stream_url_path: str | None = None
    logo_path: str | None = None
    group_path: str | None = None
    tvg_id_path: str | None = None
    default_delivery_mode: str = "direct"
    default_requires_dynamic_resolution: bool = False
    default_publishable_static: bool = True
    item_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

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

        def configured(name: str, default: Any = None) -> Any:
            env_name = data.get(f"{name}_env")
            if env_name and os.environ.get(str(env_name)) is not None:
                return os.environ[str(env_name)]
            return data.get(name, default)

        linked_query_params = configured("linked_query_params", {})
        if isinstance(linked_query_params, str):
            linked_query_params = json.loads(linked_query_params or "{}")
        linked_query_params = as_string_dict(linked_query_params)

        item_overrides = configured("item_overrides", {})
        if isinstance(item_overrides, str):
            item_overrides = json.loads(item_overrides or "{}")
        if not isinstance(item_overrides, dict):
            item_overrides = {}
        item_overrides = {
            str(key): value
            for key, value in item_overrides.items()
            if isinstance(value, dict)
        }

        return cls(
            id=str(data["id"]),
            provider_id=str(data.get("provider_id") or data["id"]),
            endpoint_url=str(endpoint_url),
            request_headers=request_headers,
            items_path=configured("items_path"),
            variants_field=str(data.get("variants_field", "variants")),
            timeout=int(data.get("timeout", 30)),
            linked_entries_url=configured("linked_entries_url"),
            linked_ids_path=configured("linked_ids_path"),
            linked_query_param=str(configured("linked_query_param", "id")),
            linked_query_params=linked_query_params,
            item_id_path=configured("item_id_path"),
            item_name_path=configured("item_name_path"),
            stream_url_path=configured("stream_url_path"),
            logo_path=configured("logo_path"),
            group_path=configured("group_path"),
            tvg_id_path=configured("tvg_id_path"),
            default_delivery_mode=str(configured("default_delivery_mode", "direct")),
            default_requires_dynamic_resolution=as_bool(
                configured("default_requires_dynamic_resolution"),
            ),
            default_publishable_static=as_bool(
                configured("default_publishable_static"),
                default=True,
            ),
            item_overrides=item_overrides,
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


def _configured_value(item: dict, path: str | None, *fallback_keys: str) -> Any:
    if path:
        value = _nested_value(item, path)
        if value is not None and value != "":
            return value
    return _first(item, *fallback_keys)


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
    configured_id = _configured_value(
        item,
        config.item_id_path,
        "logical_channel_id",
        "channel_id",
        "id",
        "slug",
        "name",
    )
    if configured_id is None:
        raise RuntimeError("JSON channel catalog item has no channel ID")
    base_id = str(configured_id)
    override = config.item_overrides.get(base_id, {})
    logical_channel_id = f"{config.provider_id}.{slugify(base_id)}"
    base_name = str(
        override.get("name")
        or _configured_value(item, config.item_name_path, "name", "title", "display_name")
        or base_id
    )
    url_value = _configured_value(
        variant,
        config.stream_url_path,
        "stream_url",
        "url",
        "playback_url",
        "hls_url",
        "dash_url",
    )
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
    drm_value = override.get("drm") or _first(variant, "drm") or _first(item, "drm")
    drm = drm_value if isinstance(drm_value, dict) else None
    delivery_mode = str(
        _first(variant, "delivery_mode", "deliveryMode")
        or _first(item, "delivery_mode", "deliveryMode")
        or config.default_delivery_mode
    ).casefold()
    dynamic = as_bool(
        _first(variant, "requires_dynamic_resolution"),
        as_bool(
            _first(item, "requires_dynamic_resolution"),
            config.default_requires_dynamic_resolution,
        ),
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
    publishable_static = default_publishable and as_bool(
        publishable_value,
        default=config.default_publishable_static,
    )

    logo = override.get("logo") or _configured_value(
        item,
        config.logo_path,
        "logo",
        "logo_url",
        "poster",
        "image",
    )
    group = override.get("group") or _configured_value(
        item,
        config.group_path,
        "category",
        "categories",
        "group",
        "genre",
        "genres",
        "category_name",
    )
    tvg_id = override.get("tvg_id") or _configured_value(
        item,
        config.tvg_id_path,
        "tvg_id",
        "tvgId",
        "xmltv_id",
        "epg_id",
    )

    return Channel(
        id=f"{config.id}.{slugify(base_id)}.{slugify(variant_id)}",
        name=display_name_with_variant(base_name, variant_label),
        source_url=f"config://{config.provider_id}/{slugify(base_id)}",
        logo=str(logo or ""),
        group=str(group or _category(item)),
        source_type=SOURCE_TYPE,
        provider_id=config.provider_id,
        logical_channel_id=logical_channel_id,
        variant_id=variant_id,
        variant_label=variant_label,
        tvg_id=tvg_id,
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
                payload = response.json()
                if config.linked_entries_url:
                    linked_ids = _nested_value(payload, config.linked_ids_path)
                    if not isinstance(linked_ids, list) or not linked_ids:
                        raise RuntimeError("Linked JSON channel catalog IDs is not a non-empty list")
                    linked_response = session.get(
                        config.linked_entries_url,
                        headers={"Accept": "application/json", **config.request_headers},
                        params={
                            **config.linked_query_params,
                            config.linked_query_param: ",".join(str(value) for value in linked_ids),
                        },
                        timeout=config.timeout,
                    )
                    linked_response.raise_for_status()
                    payload = linked_response.json()
                items = _catalog_items(payload, config.items_path)
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
