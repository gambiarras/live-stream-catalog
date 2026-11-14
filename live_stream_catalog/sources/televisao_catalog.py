"""Bounded, opt-in discovery of reviewed Televisão.TV channel players.

The public index is metadata, not a playlist. An inaccessible/unsupported
iframe stays a non-publishable discovery candidate instead of becoming a
media URL. No JavaScript, nested third-party iframe or challenge is executed.
"""

import hashlib
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urljoin, urlsplit

import requests

from live_stream_catalog.models import Channel
from live_stream_catalog.models.channel import SENSITIVE_QUERY_NAMES
from live_stream_catalog.services.expiry import extract_expiry_from_stream_url
from live_stream_catalog.sources.channel_contract import infer_protocol, is_explicit_adult


logger = logging.getLogger(__name__)
BASE_URL = "https://televisao.tv/"
INDEX_URL = BASE_URL + "channels-data.php"
SOURCE_TYPE = "televisao"
MAX_REVIEWED_CHANNELS = 24
MAX_RESPONSE_BYTES = 2_000_000
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MEDIA_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+\.(?:m3u8|mpd|mp4)(?:\?[^\s\"'<>\\]*)?", re.I)
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True, slots=True)
class TelevisaoCatalogConfig:
    slugs: tuple[str, ...]
    reviewed_stream_hosts: tuple[str, ...] = ()
    exclude_slugs: tuple[str, ...] = ()
    timeout: int = 15
    cache_dir: Path | None = None
    cache_ttl: int = 21_600
    cache_lkg: int = 86_400

    def __post_init__(self) -> None:
        if not self.slugs or len(self.slugs) > MAX_REVIEWED_CHANNELS:
            raise ValueError("Televisao discovery requires 1..24 reviewed channel slugs")
        if any(not SLUG_PATTERN.fullmatch(slug) for slug in self.slugs + self.exclude_slugs):
            raise ValueError("Invalid Televisao channel slug")
        if self.timeout <= 0 or self.cache_ttl < 0 or self.cache_lkg < self.cache_ttl:
            raise ValueError("Invalid Televisao timeout or cache lifetime")


def config_from_environment() -> TelevisaoCatalogConfig | None:
    """Disabled unless the operator supplies an explicit, reviewed slug list."""
    raw_slugs = os.environ.get("LIVE_TELEVISAO_SLUGS", "")
    slugs = tuple(dict.fromkeys(value.strip() for value in raw_slugs.split(",") if value.strip()))
    if not slugs:
        return None

    def csv(name: str) -> tuple[str, ...]:
        values = os.environ.get(name, "").split(",")
        return tuple(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))

    cache_dir = os.environ.get("LIVE_TELEVISAO_CACHE_DIR", ".cache/televisao")
    return TelevisaoCatalogConfig(
        slugs=slugs,
        reviewed_stream_hosts=csv("LIVE_TELEVISAO_REVIEWED_STREAM_HOSTS"),
        exclude_slugs=csv("LIVE_TELEVISAO_EXCLUDE"),
        timeout=int(os.environ.get("LIVE_TELEVISAO_TIMEOUT", "15")),
        cache_dir=Path(cache_dir) if cache_dir else None,
    )


def _same_origin_url(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    url = urljoin(BASE_URL, value)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "televisao.tv":
        return None
    return url


def _safe_media_url(url: str, config: TelevisaoCatalogConfig) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
        return False
    if parsed.hostname not in config.reviewed_stream_hosts:
        return False
    sensitive_names = SENSITIVE_QUERY_NAMES | {"expire", "expires", "exp"}
    if any(key.casefold() in sensitive_names for key, _ in parse_qsl(parsed.query)):
        return False
    if not parsed.path.casefold().endswith((".m3u8", ".mpd", ".mp4")):
        return False
    expires_at, _ = extract_expiry_from_stream_url(url)
    return expires_at is None


def _get_text(session: requests.Session, url: str, timeout: int, **kwargs) -> str:
    """Cap actual bytes, including when the server ignores Content-Length."""
    response = session.get(url, timeout=timeout, stream=True, allow_redirects=False, **kwargs)
    try:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError("Televisao discovery does not follow redirects")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(65_536):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ValueError("Televisao response exceeds byte limit")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        response.close()


class _PlayerParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"source", "video", "iframe"} and values.get("src"):
            self.urls.append(values["src"] or "")


def _youtube_page(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if host in {"www.youtube.com", "youtube.com", "www.youtube-nocookie.com", "youtube-nocookie.com"}:
        if parsed.path.startswith("/embed/"):
            video_id = parsed.path.split("/")[2]
        elif parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            return None
    elif host == "youtu.be":
        video_id = parsed.path.strip("/")
    else:
        return None
    if YOUTUBE_ID_PATTERN.fullmatch(video_id):
        return "https://www.youtube.com/watch?v=" + video_id
    return None


def _apply_player(channel: Channel, player_html: str, config: TelevisaoCatalogConfig) -> None:
    parser = _PlayerParser()
    parser.feed(player_html)
    text = html.unescape(player_html).replace("\\/", "/")
    # Accept only literal URLs. Executable JS/obfuscated configuration is not evaluated.
    media_urls = MEDIA_PATTERN.findall(text)
    for candidate in parser.urls + media_urls:
        url = html.unescape(candidate)
        youtube_page = _youtube_page(url)
        if youtube_page:
            channel.source_type = "youtube"
            channel.source_url = youtube_page
            channel.delivery_mode = "platform"
            return
        if _safe_media_url(url, config):
            channel.stream_url = url
            channel.status = "resolved"
            channel.requires_dynamic_resolution = False
            channel.publishable_static = True
            channel.delivery_mode = "direct"
            channel.protocol = infer_protocol(url)
            return


def _cache_path(config: TelevisaoCatalogConfig) -> Path | None:
    if config.cache_dir is None:
        return None
    fingerprint = hashlib.sha256(json.dumps({
        "version": 1,
        "slugs": config.slugs,
        "exclude": config.exclude_slugs,
        "hosts": config.reviewed_stream_hosts,
    }, sort_keys=True).encode()).hexdigest()[:20]
    return config.cache_dir / (fingerprint + ".json")


def _read_cache(config: TelevisaoCatalogConfig) -> tuple[list[Channel], float] | None:
    path = _cache_path(config)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(payload["fetched_at"])
        if age < 0 or age > config.cache_lkg:
            return None
        return [Channel.from_dict(item) for item in payload["channels"]], age
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write_cache(config: TelevisaoCatalogConfig, channels: list[Channel]) -> None:
    path = _cache_path(config)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        payload = {
            "fetched_at": time.time(),
            "channels": [channel.to_dict() for channel in channels],
        }
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    except OSError:
        logger.warning("Could not persist Televisao discovery cache")


def load_televisao_channels(
    config: TelevisaoCatalogConfig,
    default_resolution: str = "best",
    session: requests.Session | None = None,
    continue_on_error: bool = True,
) -> list[Channel]:
    cached = _read_cache(config)
    if cached and cached[1] <= config.cache_ttl:
        return cached[0]
    owns_session = session is None
    session = session or requests.Session()
    try:
        params = {"exclude": ",".join(config.exclude_slugs)} if config.exclude_slugs else {}
        payload = json.loads(_get_text(session, INDEX_URL, config.timeout, params=params))
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or not isinstance(payload.get("channels"), list)
        ):
            raise ValueError("Invalid Televisao channel index")
        indexed = {item.get("slug"): item for item in payload["channels"] if isinstance(item, dict)}
        channels: list[Channel] = []
        for slug in config.slugs:
            item = indexed.get(slug)
            if not item or slug in config.exclude_slugs or is_explicit_adult(item):
                continue
            if str(item.get("type", "tv")).casefold() not in {"tv", "television"}:
                continue
            page_url = _same_origin_url(item.get("url"))
            ajax_url = _same_origin_url(item.get("ajaxUrl"))
            if not page_url or not ajax_url:
                continue
            channel = Channel(
                id="televisao." + slug,
                name=str(item.get("name") or slug),
                source_url=page_url,
                logo=_same_origin_url(item.get("logo")) or "",
                group=str(item.get("category") or "general"),
                source_type=SOURCE_TYPE,
                provider_id=SOURCE_TYPE,
                logical_channel_id="televisao." + slug,
                variant_id="televisao." + slug,
                resolution=default_resolution,
                requires_dynamic_resolution=True,
                publishable_static=False,
                delivery_mode="embed",
            )
            try:
                details = json.loads(_get_text(session, ajax_url, config.timeout))
                if details.get("ok") is not True or details.get("slug") != slug:
                    raise ValueError("Invalid Televisao player metadata")
                iframe_url = _same_origin_url(details.get("iframe"))
                if iframe_url:
                    _apply_player(channel, _get_text(session, iframe_url, config.timeout), config)
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                logger.warning("Televisao player unavailable slug=%s error_type=%s", slug, type(exc).__name__)
            channels.append(channel)
        if not channels:
            raise ValueError("No reviewed Televisao channels found")
        _write_cache(config, channels)
        return channels
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logger.warning("Televisao discovery failed error_type=%s", type(exc).__name__)
        if cached:
            return cached[0]
        if not continue_on_error:
            raise
        return []
    finally:
        if owns_session:
            session.close()


def load_configured_televisao_catalog(default_resolution: str = "best") -> list[Channel]:
    config = config_from_environment()
    if config is None:
        return []
    return load_televisao_channels(config, default_resolution=default_resolution)
