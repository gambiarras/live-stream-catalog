import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from live_stream_catalog.local_config import source_alias
from live_stream_catalog.models.public_channel import PublicChannelDTO
from live_stream_catalog.models.resolution import ResolutionInputs
from live_stream_catalog.video_platform_transport import (
    PRIVATE_HEADERS,
    PRIVATE_NAMES,
    is_public_video_platform_hls,
)

SENSITIVE_HEADER_NAMES = PRIVATE_HEADERS | {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "api-key",
    "apikey",
}
SENSITIVE_QUERY_NAMES = PRIVATE_NAMES | {
    "username",
    "password",
    "passwd",
    "apikey",
    "access_token",
    "auth",
    "authorization",
    "credential",
    "hdnea",
    "hdntl",
    "hdnts",
    "jwt",
    "key-pair-id",
    "policy",
    "session",
    "sig",
    "signature",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
}
SECRET_REF_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
PUBLIC_DRM_SCALAR_FIELDS = {
    "type",
    "scheme",
    "system",
    "key_system",
    "keySystem",
    "robustness",
    "content_id",
    "contentId",
    "pssh",
}


def _public_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    return {
        str(name): str(header_value)
        for name, header_value in value.items()
        if str(name).strip().casefold() not in SENSITIVE_HEADER_NAMES
        and not _has_sensitive_query(str(header_value))
    }


def _public_secret_refs(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(reference)
        for key, reference in value.items()
        if SECRET_REF_PATTERN.fullmatch(str(reference))
    }


def _has_sensitive_query(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if ((parsed.scheme in {"http", "https"} and not parsed.hostname)
                or (parsed.netloc and (not parsed.hostname or port == 0
                    or "\\" in url or any(char.isspace() or ord(char) < 32 for char in url)))):
            return True
        if parsed.username is not None or parsed.password is not None:
            return True
        return any(
            name.strip().casefold() in SENSITIVE_QUERY_NAMES
            for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
    except (TypeError, ValueError):
        return True


def _public_drm(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    result = {
        key: value[key]
        for key in PUBLIC_DRM_SCALAR_FIELDS
        if key in value and isinstance(value[key], (str, int, float, bool))
    }
    license_url = value.get("license_url") or value.get("licenseUrl")
    if isinstance(license_url, str) and not _has_sensitive_query(license_url):
        result["license_url"] = license_url
    license_headers = value.get("license_headers") or value.get("licenseHeaders")
    if license_headers:
        result["license_headers"] = _public_headers(license_headers)
    return result or None


def _public_error(value: str | None) -> str | None:
    if not value:
        return None
    if value in {"no_stream_found", "account_not_found", "channel_not_found", "user_not_found", "video_platform_rate_limited", "expired_transport"}:
        return value
    if value.startswith("transient_resolve_error"):
        return "transient_resolve_error"
    return "resolution_error"


def normalize_tags(value: Any) -> list[str]:
    """Keep distinct declared string tags without inventing tags from arbitrary objects."""
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(item.strip() for item in value
                              if isinstance(item, str) and item.strip()))


@dataclass(slots=True)
class Channel:
    id: str
    name: str
    source_url: str
    logo: str
    group: str
    source_type: str
    provider_id: str | None = None
    logical_channel_id: str | None = None
    variant_id: str | None = None
    variant_label: str | None = None
    tvg_id: str | None = None
    resolution: str = "best"
    codec: str | None = None
    bitrate: int | None = None
    protocol: str | None = None
    request_headers: dict[str, str] = field(default_factory=dict)
    secret_refs: dict[str, str] = field(default_factory=dict)
    stream_url: str | None = None
    status: str = "pending"
    error: str | None = None
    resolved_at: str | None = None
    expires_at: str | None = None
    ttl_seconds: int | None = None
    requires_dynamic_resolution: bool = False
    publishable_static: bool = True
    delivery_mode: str = "direct"
    drm: dict[str, Any] | None = None
    removed: bool = False
    removed_at: str | None = None
    removal_reason: str | None = None
    feed_id: str | None = None
    tags: list[str] = field(default_factory=list)
    _resolution_inputs: ResolutionInputs | None = field(default=None, repr=False, compare=False)
    _source_instance: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.provider_id is None:
            self.provider_id = self.source_type
        if self.logical_channel_id is None:
            self.logical_channel_id = self.id
        if self.variant_id is None:
            self.variant_id = self.id

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_resolution: str = "best") -> "Channel":
        headers = data.get("request_headers") or data.get("headers")
        stream_url = data.get("stream_url")
        if headers is not None and (
            not isinstance(headers, dict) or len(_public_headers(headers)) != len(headers)
        ):
            # Discarding required private headers cannot make their media transport public.
            stream_url = None
        if source_alias(data.get("source_type")) == "video_platform" and (
            data.get("secret_refs") or data.get("drm")
            or (headers is not None and not isinstance(headers, dict))
            or (isinstance(headers, dict) and any(
                str(name).strip().casefold() in PRIVATE_HEADERS for name in headers
            ))
        ):
            # Removing private header values must not promote their transport to public media.
            stream_url = None
        return cls(
            id=data["id"],
            name=data["name"],
            source_url=data.get("source_url", data.get("url", "")),
            logo=data.get("logo", ""),
            group=data.get("group", "general"),
            source_type=source_alias(data.get("source_type", "unknown")),
            provider_id=source_alias(data.get("provider_id") or data.get("source_type", "unknown"), provider=True),
            logical_channel_id=data.get("logical_channel_id") or data.get("id"),
            variant_id=data.get("variant_id"),
            variant_label=data.get("variant_label"),
            tvg_id=data.get("tvg_id"),
            resolution=data.get("resolution", default_resolution),
            codec=data.get("codec"),
            bitrate=data.get("bitrate"),
            protocol=data.get("protocol"),
            request_headers=_public_headers(data.get("request_headers") or data.get("headers")),
            secret_refs=_public_secret_refs(data.get("secret_refs")),
            stream_url=stream_url,
            status=data.get("status", "pending"),
            error=data.get("error"),
            resolved_at=data.get("resolved_at"),
            expires_at=data.get("expires_at"),
            ttl_seconds=data.get("ttl_seconds"),
            requires_dynamic_resolution=bool(data.get("requires_dynamic_resolution", False)),
            publishable_static=bool(data.get("publishable_static", True)),
            delivery_mode=data.get("delivery_mode", "direct"),
            drm=data.get("drm") if isinstance(data.get("drm"), dict) else None,
            removed=bool(data.get("removed", False)),
            removed_at=data.get("removed_at"),
            removal_reason=data.get("removal_reason"),
            feed_id=data.get("feed_id"),
            tags=normalize_tags(data.get("tags")),
        )

    def declared_inputs(self) -> ResolutionInputs:
        """Capture inputs once, before reconciliation or resolver mutation."""
        if self._resolution_inputs is None:
            self._resolution_inputs = ResolutionInputs.from_channel(self)
        return self._resolution_inputs

    def to_state_dict(self) -> dict[str, Any]:
        """Serialize complete private state, including private request context."""
        declared = self.declared_inputs()
        payload = asdict(self)
        payload.pop("_resolution_inputs")
        payload["resolution_inputs"] = declared.to_dict()
        payload["source_instance"] = payload.pop("_source_instance")
        return payload

    @classmethod
    def from_state_dict(cls, data: dict[str, Any], default_resolution: str = "best") -> "Channel":
        """Restore trusted private state without passing it through the public sanitizer."""
        if not isinstance(data, dict):
            raise TypeError("Invalid private channel entry")
        if any(not isinstance(data.get(key), str) for key in ("id", "name", "source_type")):
            raise TypeError("Invalid private channel identity")
        for key in ("request_headers", "secret_refs"):
            if key in data and not isinstance(data[key], dict):
                raise TypeError("Invalid private channel request context")
        if data.get("source_instance") is not None and not isinstance(data["source_instance"], str):
            raise TypeError("Invalid private channel source instance")
        if data.get("resolution_inputs") is not None and not isinstance(data["resolution_inputs"], dict):
            raise TypeError("Invalid private channel declared inputs")
        channel = cls.from_dict(data, default_resolution=default_resolution)
        channel.source_url = data.get("source_url", data.get("url", ""))
        channel.stream_url = data.get("stream_url")
        channel.request_headers = {str(key): str(value) for key, value in
                                   (data.get("request_headers") or {}).items()}
        channel.secret_refs = {str(key): str(value) for key, value in
                              (data.get("secret_refs") or {}).items()}
        channel.drm = deepcopy(data.get("drm"))
        channel._source_instance = data.get("source_instance")
        declared = data.get("resolution_inputs")
        if isinstance(declared, dict):
            channel._resolution_inputs = ResolutionInputs.from_dict(declared)
        return channel

    def to_public_dto(self) -> PublicChannelDTO:
        payload = asdict(self)
        payload.pop("_resolution_inputs")
        payload.pop("_source_instance")
        payload["request_headers"] = _public_headers(self.request_headers)
        payload["secret_refs"] = _public_secret_refs(self.secret_refs)
        payload["error"] = _public_error(self.error)
        if _has_sensitive_query(self.source_url):
            payload["source_url"] = None
        withheld_headers = len(payload["request_headers"]) != len(self.request_headers)
        renewable_hls = not withheld_headers and is_public_video_platform_hls(self)
        if self.source_type == "video_platform" and not renewable_hls:
            payload["stream_url"] = None
        elif not renewable_hls and (
            _has_sensitive_query(self.stream_url)
            or self.secret_refs
            or withheld_headers
            or any(str(name).strip().casefold() in SENSITIVE_HEADER_NAMES for name in self.request_headers)
            or self.delivery_mode in {"dai", "ssai"}
            or self.drm is not None
            or (not self.publishable_static and self.requires_dynamic_resolution)
        ):
            payload["stream_url"] = None
        payload["drm"] = _public_drm(self.drm)
        return PublicChannelDTO.from_payload(payload)

    def to_dict(self) -> dict[str, Any]:
        """Backward-compatible public array entry."""
        return self.to_public_dto().to_dict()
