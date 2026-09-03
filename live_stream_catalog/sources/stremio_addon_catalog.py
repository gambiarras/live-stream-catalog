import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib import resources
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
from requests.adapters import HTTPAdapter

from live_stream_catalog.io import write_json_atomic
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
    max_workers: int = 4
    cache_dir: Path | None = None
    cache_ttl: int = 21600
    cache_lkg: int = 172800

    @classmethod
    def from_dict(cls, data: dict) -> "StremioAddonCatalogConfig":
        manifest_url = data.get("manifest_url")
        manifest_url_env = data.get("manifest_url_env")
        if manifest_url_env:
            manifest_url = os.environ.get(str(manifest_url_env), manifest_url)
        if not manifest_url:
            raise ValueError(f"Missing addon manifest URL for id={data.get('id')}")

        def configured_int(key: str, default: int, minimum: int = 1) -> int:
            value = data.get(key, default)
            env_name = data.get(f"{key}_env")
            if env_name:
                value = os.environ.get(str(env_name), value)
            return max(minimum, int(value))

        cache_dir = data.get("cache_dir")
        cache_dir_env = data.get("cache_dir_env")
        if cache_dir_env:
            cache_dir = os.environ.get(str(cache_dir_env), cache_dir)

        return cls(
            id=str(data["id"]),
            provider_id=str(data.get("provider_id") or data["id"]),
            manifest_url=str(manifest_url),
            types=tuple(str(item) for item in data.get("types", ("channel", "tv"))),
            catalog_ids=tuple(str(item) for item in data.get("catalog_ids", ())),
            timeout=configured_int("timeout", 30),
            max_items=configured_int("max_items", 1000),
            page_size=configured_int("page_size", 100),
            max_workers=configured_int("max_workers", 4),
            cache_dir=Path(str(cache_dir)) if cache_dir else None,
            cache_ttl=configured_int("cache_ttl", 21600, minimum=0),
            cache_lkg=configured_int("cache_lkg", 172800, minimum=0),
        )


def _cache_path(config: StremioAddonCatalogConfig) -> Path | None:
    if config.cache_dir is None:
        return None
    return config.cache_dir / f"{slugify(config.id)}.json"


def _checkpoint_path(config: StremioAddonCatalogConfig) -> Path | None:
    path = _cache_path(config)
    if path is None:
        return None
    return path.with_suffix(".checkpoint.json")


def _cache_fingerprint(config: StremioAddonCatalogConfig) -> str:
    value = "|".join((config.id, config.provider_id, config.manifest_url, *config.types, *config.catalog_ids))
    return sha256(value.encode("utf-8")).hexdigest()


def _load_cached_channels(
    config: StremioAddonCatalogConfig,
    default_resolution: str,
) -> tuple[list[Channel], float] | None:
    path = _cache_path(config)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("fingerprint") != _cache_fingerprint(config):
            return None
        fetched_at = datetime.fromisoformat(str(payload["fetched_at"]).replace("Z", "+00:00"))
        age = max(0.0, (datetime.now(timezone.utc) - fetched_at).total_seconds())
        channels = [
            Channel.from_dict(item, default_resolution=default_resolution)
            for item in payload.get("channels", [])
            if isinstance(item, dict)
        ]
        return (channels, age) if channels else None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        logger.warning(
            "Ignoring invalid addon cache id=%s error_type=%s",
            config.id,
            type(exc).__name__,
        )
        return None


def _write_cached_channels(config: StremioAddonCatalogConfig, channels: list[Channel]) -> None:
    path = _cache_path(config)
    if path is None:
        return
    write_json_atomic(
        path,
        {
            "version": 1,
            "fingerprint": _cache_fingerprint(config),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "channels": [channel.to_dict() for channel in channels],
        },
    )


def _load_checkpoint_channels(
    config: StremioAddonCatalogConfig,
    default_resolution: str,
) -> dict[str, list[Channel]]:
    path = _checkpoint_path(config)
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("fingerprint") != _cache_fingerprint(config):
            return {}
        updated_at = datetime.fromisoformat(str(payload["updated_at"]).replace("Z", "+00:00"))
        age = max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds())
        if age > config.cache_lkg:
            return {}
        items = payload.get("items", {})
        if not isinstance(items, dict):
            return {}
        result = {
            str(key): [
                Channel.from_dict(item, default_resolution=default_resolution)
                for item in value
                if isinstance(item, dict)
            ]
            for key, value in items.items()
            if isinstance(value, list)
        }
        if result:
            logger.info(
                "Loaded addon checkpoint id=%s items=%d age=%.0fs",
                config.id,
                len(result),
                age,
            )
        return result
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        logger.warning(
            "Ignoring invalid addon checkpoint id=%s error_type=%s",
            config.id,
            type(exc).__name__,
        )
        return {}


def _write_checkpoint_channels(
    config: StremioAddonCatalogConfig,
    items: dict[str, list[Channel]],
) -> None:
    path = _checkpoint_path(config)
    if path is None or not items:
        return
    write_json_atomic(
        path,
        {
            "version": 1,
            "fingerprint": _cache_fingerprint(config),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "items": {
                key: [channel.to_dict() for channel in channels]
                for key, channels in items.items()
            },
        },
    )


def _clear_checkpoint(config: StremioAddonCatalogConfig) -> None:
    path = _checkpoint_path(config)
    if path is not None:
        path.unlink(missing_ok=True)


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


def _meta_checkpoint_key(catalog: dict, meta: dict) -> str:
    identity = json.dumps(
        [
            catalog.get("type"),
            catalog.get("id"),
            meta.get("id"),
            _meta_video_ids(meta),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(identity.encode("utf-8")).hexdigest()


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


def _load_meta_channels(
    session: requests.Session,
    config: StremioAddonCatalogConfig,
    catalog: dict,
    meta: dict,
    default_resolution: str,
) -> tuple[list[Channel], bool]:
    channels: list[Channel] = []
    failed = False
    for video_id in _meta_video_ids(meta):
        endpoint = resource_url(
            config.manifest_url,
            "stream",
            str(meta.get("type") or catalog["type"]),
            video_id,
        )
        try:
            payload = _request_json(session, endpoint, config.timeout)
        except Exception as exc:
            failed = True
            logger.warning(
                "Failed to load addon stream id=%s error_type=%s",
                config.id,
                type(exc).__name__,
            )
            continue
        streams = payload.get("streams", [])
        if not isinstance(streams, list):
            failed = True
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
    return channels, failed


def _load_catalog_channels(
    session: requests.Session,
    config: StremioAddonCatalogConfig,
    catalog: dict,
    default_resolution: str,
    checkpoint_items: dict[str, list[Channel]],
) -> tuple[list[Channel], bool]:
    metas = [
        meta
        for meta in _load_catalog_metas(session, config, catalog)
        if not is_explicit_adult(meta)
    ]
    if not metas:
        return [], False

    ordered: dict[int, list[Channel]] = {}
    pending: list[tuple[int, dict, str]] = []
    for index, meta in enumerate(metas):
        checkpoint_key = _meta_checkpoint_key(catalog, meta)
        if checkpoint_key in checkpoint_items:
            ordered[index] = checkpoint_items[checkpoint_key]
        else:
            pending.append((index, meta, checkpoint_key))

    resumed = len(metas) - len(pending)
    if resumed:
        logger.info(
            "Resuming addon catalog id=%s catalog=%s cached=%d remaining=%d",
            config.id,
            catalog.get("id"),
            resumed,
            len(pending),
        )
    if not pending:
        return [
            channel
            for index in sorted(ordered)
            for channel in ordered[index]
        ], False

    worker_count = min(config.max_workers, len(pending))
    failed = False
    started_at = time.monotonic()
    progress_interval = max(25, len(metas) // 10)
    logger.info(
        "Loading addon items id=%s catalog=%s total=%d workers=%d request_timeout=%ds",
        config.id,
        catalog.get("id"),
        len(metas),
        worker_count,
        config.timeout,
    )
    checkpoint_dirty = False
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _load_meta_channels,
                session,
                config,
                catalog,
                meta,
                default_resolution,
            ): (index, checkpoint_key)
            for index, meta, checkpoint_key in pending
        }
        for completed, future in enumerate(as_completed(futures), start=resumed + 1):
            index, checkpoint_key = futures[future]
            try:
                ordered[index], item_failed = future.result()
                failed = failed or item_failed
                if not item_failed:
                    checkpoint_items[checkpoint_key] = ordered[index]
                    checkpoint_dirty = True
            except Exception as exc:
                failed = True
                logger.warning(
                    "Failed to process addon item id=%s error_type=%s",
                    config.id,
                    type(exc).__name__,
                )
                ordered[index] = []
            if completed % progress_interval == 0 or completed == len(metas):
                if checkpoint_dirty:
                    _write_checkpoint_channels(config, checkpoint_items)
                    checkpoint_dirty = False
                logger.info(
                    "Addon progress id=%s catalog=%s completed=%d/%d elapsed=%.1fs",
                    config.id,
                    catalog.get("id"),
                    completed,
                    len(metas),
                    time.monotonic() - started_at,
                )

    if checkpoint_dirty:
        _write_checkpoint_channels(config, checkpoint_items)

    return [
        channel
        for index in sorted(ordered)
        for channel in ordered[index]
    ], failed


def load_stremio_addon_channels(
    configs: list[StremioAddonCatalogConfig],
    default_resolution: str = "best",
    session: requests.Session | None = None,
    continue_on_error: bool = True,
) -> list[Channel]:
    owns_session = session is None
    if session is None:
        session = requests.Session()
        pool_size = max((config.max_workers for config in configs), default=1)
        adapter = HTTPAdapter(
            pool_connections=pool_size,
            pool_maxsize=pool_size,
            pool_block=True,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    channels: list[Channel] = []

    try:
        for config in configs:
            cached = _load_cached_channels(config, default_resolution)
            if cached and cached[1] <= config.cache_ttl:
                logger.info(
                    "Using fresh addon cache id=%s channels=%d age=%.0fs ttl=%ds",
                    config.id,
                    len(cached[0]),
                    cached[1],
                    config.cache_ttl,
                )
                _clear_checkpoint(config)
                channels.extend(cached[0])
                continue
            checkpoint_items = _load_checkpoint_channels(config, default_resolution)
            try:
                manifest = _request_json(session, config.manifest_url, config.timeout)
                if is_explicit_adult(manifest.get("behaviorHints", {})):
                    logger.warning("Explicitly adult addon skipped id=%s", config.id)
                    continue

                catalogs = manifest.get("catalogs", [])
                if not isinstance(catalogs, list):
                    raise RuntimeError("Addon manifest catalogs is not a list")

                config_channels: list[Channel] = []
                had_failures = False
                for catalog in catalogs:
                    if not isinstance(catalog, dict) or not _catalog_is_selected(catalog, config):
                        continue
                    catalog_channels, catalog_failed = _load_catalog_channels(
                        session,
                        config,
                        catalog,
                        default_resolution,
                        checkpoint_items,
                    )
                    config_channels.extend(catalog_channels)
                    had_failures = had_failures or catalog_failed

                if had_failures and cached and cached[1] <= config.cache_lkg:
                    logger.warning(
                        "Using last-known-good addon cache after transient item failures "
                        "id=%s channels=%d age=%.0fs",
                        config.id,
                        len(cached[0]),
                        cached[1],
                    )
                    channels.extend(cached[0])
                else:
                    channels.extend(config_channels)
                    if config_channels and not had_failures:
                        _write_cached_channels(config, config_channels)
                        _clear_checkpoint(config)
            except Exception as exc:
                if cached and cached[1] <= config.cache_lkg:
                    logger.warning(
                        "Using last-known-good addon cache after source failure "
                        "id=%s channels=%d age=%.0fs error_type=%s",
                        config.id,
                        len(cached[0]),
                        cached[1],
                        type(exc).__name__,
                    )
                    channels.extend(cached[0])
                    continue
                logger.error(
                    "Failed to load addon catalog id=%s error_type=%s",
                    config.id,
                    type(exc).__name__,
                )
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
