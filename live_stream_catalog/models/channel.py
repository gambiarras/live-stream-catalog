from dataclasses import asdict, dataclass, field
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit


SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "api-key",
}
SENSITIVE_QUERY_NAMES = {
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
        if str(name).casefold() not in SENSITIVE_HEADER_NAMES
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
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        return True
    return any(name.casefold() in SENSITIVE_QUERY_NAMES for name, _ in parse_qsl(parsed.query))


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
    if value in {"no_stream_found", "account_not_found", "channel_not_found", "user_not_found"}:
        return value
    if value.startswith("transient_resolve_error"):
        return "transient_resolve_error"
    return "resolution_error"


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

    def __post_init__(self) -> None:
        if self.provider_id is None:
            self.provider_id = self.source_type
        if self.logical_channel_id is None:
            self.logical_channel_id = self.id
        if self.variant_id is None:
            self.variant_id = self.id

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_resolution: str = "best") -> "Channel":
        return cls(
            id=data["id"],
            name=data["name"],
            source_url=data.get("source_url", data.get("url", "")),
            logo=data.get("logo", ""),
            group=data.get("group", "general"),
            source_type=data.get("source_type", "unknown"),
            provider_id=data.get("provider_id") or data.get("source_type", "unknown"),
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
            stream_url=data.get("stream_url"),
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
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["request_headers"] = _public_headers(self.request_headers)
        payload["secret_refs"] = _public_secret_refs(self.secret_refs)
        payload["error"] = _public_error(self.error)
        if _has_sensitive_query(self.source_url):
            payload["source_url"] = None
        if not self.publishable_static and _has_sensitive_query(self.stream_url):
            payload["stream_url"] = None
        payload["drm"] = _public_drm(self.drm)
        return payload
