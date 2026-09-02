import hashlib
import re
import unicodedata
from typing import Any
from urllib.parse import urlparse


EXPLICIT_ADULT_PATTERN = re.compile(
    r"(?:^|[\s(/_-])(?:\+18|18\+|adultos?|adults?|nsfw|xxx)(?:$|[\s)/_-])",
    re.IGNORECASE,
)
QUALITY_TOKEN_PATTERN = re.compile(
    r"(?<![a-z0-9])(2160p|4k|uhd|1080p|full\s*hd|fhd|720p|hd|576p|540p|480p|360p|sd)(?![a-z0-9])",
    re.IGNORECASE,
)
HEVC_PATTERN = re.compile(r"(?<![a-z0-9])(hevc|h\.?265)(?![a-z0-9])", re.IGNORECASE)
TRAILING_VARIANT_PATTERN = re.compile(
    r"\s*(?:[-–|]\s*)?(?:\[?\s*(?:2160p|4k|uhd|1080p|full\s*hd|fhd|720p|hd|576p|540p|480p|360p|sd)\s*\]?)"
    r"(?:\s*\[?\s*(?:hevc|h\.?265)\s*\]?)?\s*$",
    re.IGNORECASE,
)


def slugify(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")
    return slug or "channel"


def infer_protocol(url: str | None, explicit: str | None = None) -> str | None:
    if explicit:
        return str(explicit).casefold()
    if not url:
        return None

    path = urlparse(url).path.casefold()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    if path.endswith(".mp4"):
        return "http"
    return urlparse(url).scheme.casefold() or None


def normalize_variant_label(
    *values: Any,
    resolution: str | None = None,
    codec: str | None = None,
) -> str | None:
    combined = " ".join(str(value) for value in values if value)
    resolution_text = str(resolution or "")
    match = QUALITY_TOKEN_PATTERN.search(f"{combined} {resolution_text}")
    quality: str | None = None
    if match:
        token = re.sub(r"\s+", "", match.group(1).casefold())
        if token in {"2160p", "4k", "uhd"}:
            quality = "4K"
        elif token in {"1080p", "fullhd", "fhd"}:
            quality = "FHD"
        elif token in {"720p", "hd"}:
            quality = "HD"
        elif token in {"576p", "540p", "480p", "360p", "sd"}:
            quality = "SD"

    hevc = bool(HEVC_PATTERN.search(f"{combined} {codec or ''}"))
    parts = [part for part in (quality, "HEVC" if hevc else None) if part]
    return " ".join(parts) or None


def display_name_with_variant(name: str, variant_label: str | None) -> str:
    if not variant_label:
        return name

    base_name = TRAILING_VARIANT_PATTERN.sub("", name).strip() or name
    suffixes = "".join(f" [{part}]" for part in variant_label.split())
    return f"{base_name}{suffixes}"


def stable_variant_id(
    provider_id: str,
    logical_channel_id: str,
    variant_label: str | None,
    position: int,
    url: str | None,
) -> str:
    label = slugify(variant_label or f"variant-{position + 1}")
    digest = hashlib.sha1((url or str(position)).encode("utf-8")).hexdigest()[:8]
    return f"{slugify(provider_id)}.{slugify(logical_channel_id)}.{label}.{digest}"


def is_explicit_adult(item: dict[str, Any]) -> bool:
    for key in ("adult", "is_adult", "isAdult", "is_nsfw", "nsfw"):
        if item.get(key) is True:
            return True

    values: list[Any] = []
    for key in ("rating", "content_rating", "contentRating", "genre", "genres", "category", "categories"):
        value = item.get(key)
        if isinstance(value, dict):
            values.extend(value.values())
        elif isinstance(value, list):
            values.extend(value)
        elif value is not None:
            values.append(value)

    return any(EXPLICIT_ADULT_PATTERN.search(str(value).strip()) for value in values)


def as_string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
