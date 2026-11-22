"""Declared resolver inputs, independent of a transport's mutable observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from live_stream_catalog.models.channel import Channel


@dataclass(frozen=True, slots=True)
class ResolutionInputs:
    """Capture only the declared identity, request context and capability requirements."""

    source_url: str
    source_type: str
    provider_id: str | None
    logical_channel_id: str | None
    variant_id: str | None
    feed_id: str | None
    resolution: str
    codec: str | None
    protocol: str | None
    request_headers: tuple[tuple[str, str], ...]
    secret_refs: tuple[tuple[str, str], ...]
    requires_dynamic_resolution: bool
    publishable_static: bool
    delivery_mode: str
    drm_json: str | None

    @classmethod
    def from_channel(cls, channel: Channel) -> ResolutionInputs:
        return cls(
            source_url=channel.source_url,
            source_type=channel.source_type,
            provider_id=channel.provider_id,
            logical_channel_id=channel.logical_channel_id,
            variant_id=channel.variant_id,
            feed_id=channel.feed_id,
            resolution=channel.resolution,
            codec=channel.codec,
            protocol=channel.protocol,
            request_headers=tuple(
                sorted(
                    (str(key).strip().casefold(), str(value))
                    for key, value in channel.request_headers.items()
                )
            ),
            secret_refs=tuple(
                sorted(
                    (str(key), str(value)) for key, value in channel.secret_refs.items()
                )
            ),
            requires_dynamic_resolution=channel.requires_dynamic_resolution,
            publishable_static=channel.publishable_static,
            delivery_mode=channel.delivery_mode,
            drm_json=(
                json.dumps(channel.drm, sort_keys=True, separators=(",", ":"))
                if channel.drm is not None
                else None
            ),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResolutionInputs:
        data = dict(value)
        for key in ("request_headers", "secret_refs"):
            pairs = data[key]
            if not isinstance(pairs, (list, tuple)) or any(
                not isinstance(pair, (list, tuple))
                or len(pair) != 2
                or not all(isinstance(item, str) for item in pair)
                for pair in pairs
            ):
                raise TypeError("Invalid declared request context")
            data[key] = tuple(tuple(pair) for pair in pairs)
        for key in ("source_type", "resolution", "delivery_mode"):
            if not isinstance(data[key], str):
                raise TypeError("Invalid declared source context")
        for key in ("requires_dynamic_resolution", "publishable_static"):
            if not isinstance(data[key], bool):
                raise TypeError("Invalid declared capability")
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        """Return a private comparison key; it must never enter public metadata."""
        value = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
