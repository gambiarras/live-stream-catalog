"""Shared reconciliation, resolution and publication for build and refresh."""

import hashlib
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from live_stream_catalog.config import AppConfig
from live_stream_catalog.io import load_channels_file, write_json_atomic
from live_stream_catalog.io.catalog_state_store import (
    CatalogSnapshot,
    load_private_state,
    private_state_path,
    save_private_state,
)
from live_stream_catalog.models import Channel, RunMetadata
from live_stream_catalog.services.catalog_state import (
    carry_previous_resolution,
    split_refresh_candidates,
)
from live_stream_catalog.services.discovery import DiscoveryCache, DiscoveryContext
from live_stream_catalog.services.metadata import build_run_metadata
from live_stream_catalog.services.resolver import resolve_channels
from live_stream_catalog.services.video_platform_policy import (
    VideoPlatformRequestPolicy,
)


@dataclass(slots=True)
class CatalogRun:
    config: AppConfig
    state_path: Path
    previous: CatalogSnapshot
    discovery: DiscoveryContext
    video_platform_policy: VideoPlatformRequestPolicy


class CatalogResolver(Protocol):
    def __call__(
        self,
        channels: list[Channel],
        max_workers: int,
        *,
        video_platform_policy: VideoPlatformRequestPolicy | None = None,
    ) -> list[Channel]: ...


def prepare_catalog_run(config: AppConfig) -> CatalogRun:
    path = private_state_path(config.output_path)
    cache_override = os.environ.get("LIVE_DISCOVERY_CACHE_DIR")
    cache_root = Path(cache_override) if cache_override else path.parent / "discovery"
    publication_paths = {
        config.output_path.resolve(),
        config.meta_output_path.resolve(),
    }
    if any(private.resolve() in publication_paths for private in (path, cache_root)):
        raise ValueError(
            "Private catalog storage must not collide with a publication path"
        )
    snapshot = load_private_state(path, default_resolution=config.default_resolution)
    if snapshot is None:
        snapshot = CatalogSnapshot(
            load_channels_file(config.output_path, config.default_resolution)
        )
    context = DiscoveryContext(DiscoveryCache(cache_root), snapshot.discovery)
    return CatalogRun(
        config,
        path,
        snapshot,
        context,
        VideoPlatformRequestPolicy.for_output(config.output_path),
    )


def _withheld_reason(channel: Channel, payload: dict[str, Any]) -> str | None:
    if (
        payload.get("stream_url")
        and channel.status == "resolved"
        and not channel.removed
    ):
        return None
    if channel.error == "video_platform_rate_limited":
        return "rate_limited"
    if channel.removed or channel.status == "removed":
        return "removed"
    if channel.status != "resolved":
        return "not_resolved"
    if not channel.stream_url:
        return "no_transport"
    if (
        channel.secret_refs
        or channel.drm is not None
        or channel.delivery_mode in {"dai", "ssai"}
    ):
        return "restricted_transport"
    if channel.source_type == "video_platform":
        return "renewable_media_gate"
    return "dynamic_or_sensitive_transport"


def finish_catalog_run(
    run: CatalogRun,
    candidates: list[Channel],
    *,
    sort_for_resolution: Callable[[list[Channel]], list[Channel]] | None = None,
    resolver: CatalogResolver = resolve_channels,
) -> RunMetadata:
    """Keep mode-specific discovery outside this shared, deterministic tail."""
    if not candidates:
        raise RuntimeError(
            "No configured catalog candidates; preserve the last publication"
        )
    run.discovery.complete_unrecorded(candidates)
    candidates = carry_previous_resolution(candidates, run.previous.channels)
    keep, refresh = split_refresh_candidates(candidates, run.config.min_ttl_seconds)
    if sort_for_resolution:
        refresh = sort_for_resolution(refresh)
    channels = keep + resolver(
        refresh,
        max_workers=run.config.max_workers,
        video_platform_policy=run.video_platform_policy,
    )
    if not channels:
        raise RuntimeError(
            "No configured catalog candidates; preserve the last publication"
        )
    channels.sort(key=lambda item: item.id.casefold())
    run.video_platform_policy.finish()
    payload = [channel.to_public_dto().to_dict() for channel in channels]
    reasons: Counter[str] = Counter()
    source_counts: dict[str, dict[str, int]] = {}
    for channel, public in zip(channels, payload):
        reason = _withheld_reason(channel, public)
        if reason:
            reasons[reason] += 1
        counts = source_counts.setdefault(
            channel.source_type, {"channels": 0, "resolved": 0, "public_transports": 0}
        )
        counts["channels"] += 1
        counts["resolved"] += int(channel.status == "resolved")
        counts["public_transports"] += int(reason is None)
    diagnostics = {
        "sources": run.discovery.observations,
        "resolution": {"kept": len(keep), "attempted": len(refresh)},
        "publication": {"withheld_by_reason": dict(sorted(reasons.items()))},
    }
    save_private_state(
        run.state_path, CatalogSnapshot(channels, run.discovery.observations)
    )
    write_json_atomic(run.config.output_path, payload)
    digest = hashlib.sha256(run.config.output_path.read_bytes()).hexdigest()
    metadata = build_run_metadata(
        channels,
        catalog_sha256=digest,
        diagnostics=diagnostics,
        source_counts=source_counts,
    )
    write_json_atomic(run.config.meta_output_path, metadata.to_dict())
    return metadata
