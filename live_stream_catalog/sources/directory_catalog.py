"""Bounded, opt-in discovery of reviewed directory catalog channel players.

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
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from live_stream_catalog.local_config import resource_json, source_settings
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from live_stream_catalog.models import Channel
from live_stream_catalog.models.channel import SENSITIVE_QUERY_NAMES
from live_stream_catalog.services.expiry import extract_expiry_from_stream_url
from live_stream_catalog.sources.channel_contract import infer_protocol, is_explicit_adult


logger = logging.getLogger(__name__)


def _configured_base_url() -> str:
    """Use an optional operations override without changing reviewed channel policy."""
    value = os.environ.get("LIVE_DIRECTORY_BASE_URL") or source_settings(
        "directory_catalog"
    ).get("base_url", "https://directory.example.test/")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or "?" in value or "#" in value or "\\" in value
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError("Invalid directory base URL")
        # Access validates malformed or out-of-range port syntax as well.
        parsed.port
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Invalid directory base URL; use an HTTPS base without credentials or query") from exc
    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


BASE_URL = _configured_base_url()
INDEX_URL = BASE_URL + source_settings("directory_catalog").get("index_path", "channels-data.php")
SOURCE_TYPE = "directory_catalog"
MAX_REVIEWED_CHANNELS = 24
MAX_RESPONSE_BYTES = 2_000_000
MAX_CACHE_BYTES = 8_000_000
MAX_CACHE_FILES = 4
CACHE_VERSION = 3
DISCOVERY_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HTML_ACCEPT = "text/html,application/xhtml+xml"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MEDIA_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+\.(?:m3u8|mpd|mp4)(?:\?[^\s\"'<>\\]*)?", re.I)
VIDEO_PLATFORM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True, slots=True)
class DirectoryCatalogConfig:
    slugs: tuple[str, ...]
    reviewed_stream_hosts: tuple[str, ...] = ()
    exclude_slugs: tuple[str, ...] = ()
    timeout: int = 15
    cache_dir: Path | None = None
    cache_ttl: int = 21_600
    cache_lkg: int = 86_400
    reviewed_hosts_by_slug: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        if not self.slugs or len(self.slugs) > MAX_REVIEWED_CHANNELS:
            raise ValueError("Directory discovery requires 1..24 reviewed channel slugs")
        if any(not SLUG_PATTERN.fullmatch(slug) for slug in self.slugs + self.exclude_slugs):
            raise ValueError("Invalid Directory channel slug")
        if self.timeout <= 0 or self.cache_ttl < 0 or self.cache_lkg < self.cache_ttl:
            raise ValueError("Invalid Directory timeout or cache lifetime")


def default_review() -> dict:
    """Small public-broadcaster inventory; no approval of arbitrary directory hosts."""
    return resource_json("directory_defaults.json")


def config_from_environment() -> DirectoryCatalogConfig | None:
    """Load the six-channel curated default; explicit empty slugs disable it."""
    reviewed = default_review()["channels"]
    default_slugs = ",".join(item["slug"] for item in reviewed)
    default_hosts = tuple(dict.fromkeys(host for item in reviewed for host in item["reviewed_stream_hosts"]))
    raw_slugs = os.environ.get("LIVE_DIRECTORY_SLUGS", default_slugs)
    slugs = tuple(dict.fromkeys(value.strip() for value in raw_slugs.split(",") if value.strip()))
    if not slugs:
        return None

    def csv(name: str, default: str = "") -> tuple[str, ...]:
        values = os.environ.get(name, default).split(",")
        return tuple(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))

    cache_dir = os.environ.get("LIVE_DIRECTORY_CACHE_DIR", ".cache/directory_catalog")
    hosts = csv("LIVE_DIRECTORY_REVIEWED_STREAM_HOSTS", ",".join(default_hosts))
    per_slug = tuple((item["slug"], tuple(item["reviewed_stream_hosts"])) for item in reviewed) if set(hosts) == set(default_hosts) else ()
    return DirectoryCatalogConfig(
        slugs=slugs,
        reviewed_stream_hosts=hosts,
        exclude_slugs=csv("LIVE_DIRECTORY_EXCLUDE"),
        timeout=int(os.environ.get("LIVE_DIRECTORY_TIMEOUT", "15")),
        cache_dir=Path(cache_dir) if cache_dir else None,
        reviewed_hosts_by_slug=per_slug,
    )


def _same_origin_url(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    url = urljoin(BASE_URL, value)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != urlsplit(BASE_URL).netloc:
        return None
    return url


def _safe_media_url(url: str, config: DirectoryCatalogConfig) -> bool:
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
            raise ValueError("Directory discovery does not follow redirects")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(65_536):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ValueError("Directory response exceeds byte limit")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        response.close()


def _page_headers(referer: str | None = None, *, ajax: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": DISCOVERY_USER_AGENT,
        "Accept": "application/json" if ajax else HTML_ACCEPT,
    }
    if referer:
        headers["Referer"] = referer
    if ajax:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


def _autoplay_url(url: str) -> str:
    """Mirror the site's static playerAutoplayUrl helper, without running JS."""
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("autoplay", "1")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


class _PlayerParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"source", "video", "iframe"} and values.get("src"):
            self.urls.append(values["src"] or "")


def _video_platform_page(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if host in source_settings("video_platform").get("page_hosts", ["video.example.test", "video-embed.example.test"]):
        if parsed.path.startswith("/embed/"):
            video_id = parsed.path.split("/")[2]
        elif parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            return None
    elif host in source_settings("video_platform").get("short_hosts", ["video-short.example.test"]):
        video_id = parsed.path.strip("/")
    else:
        return None
    if VIDEO_PLATFORM_ID_PATTERN.fullmatch(video_id):
        return source_settings("video_platform").get("watch_url", "https://video.example.test/watch?v={video_id}").format(video_id=video_id)
    return None


def _apply_player(channel: Channel, player_html: str, config: DirectoryCatalogConfig) -> None:
    parser = _PlayerParser()
    parser.feed(player_html)
    text = html.unescape(player_html).replace("\\/", "/")
    # Accept only literal URLs. Executable JS/obfuscated configuration is not evaluated.
    media_urls = MEDIA_PATTERN.findall(text)
    for candidate in parser.urls + media_urls:
        url = html.unescape(candidate)
        video_platform_page = _video_platform_page(url)
        if video_platform_page:
            channel.source_type = "video_platform"
            channel.source_url = video_platform_page
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


def _cache_path(config: DirectoryCatalogConfig) -> Path | None:
    if config.cache_dir is None:
        return None
    fingerprint = hashlib.sha256(json.dumps({
        "version": CACHE_VERSION,
        "base_url": BASE_URL,
        "index_url": INDEX_URL,
        "slugs": config.slugs,
        "exclude": config.exclude_slugs,
        "hosts": config.reviewed_stream_hosts,
        "hosts_by_slug": config.reviewed_hosts_by_slug,
    }, sort_keys=True).encode()).hexdigest()[:20]
    return config.cache_dir / (fingerprint + ".json")


def _read_cache(config: DirectoryCatalogConfig) -> tuple[list[Channel], float] | None:
    path = _cache_path(config)
    if path is None:
        return None
    try:
        if path.is_symlink() or path.stat().st_size > MAX_CACHE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(payload["fetched_at"])
        if age < 0 or age > config.cache_lkg:
            return None
        rows = payload["channels"]
        if not isinstance(rows, list) or len(rows) > MAX_REVIEWED_CHANNELS:
            return None
        return [Channel.from_dict(item) for item in rows], age
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write_cache(config: DirectoryCatalogConfig, channels: list[Channel]) -> None:
    path = _cache_path(config)
    if path is None:
        return
    temp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": time.time(),
            "channels": [channel.to_dict() for channel in channels],
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_CACHE_BYTES:
            logger.warning("Directory discovery cache exceeds size limit")
            return
        temp.write_bytes(raw)
        temp.replace(path)
        _prune_cache(config)
    except OSError:
        logger.warning("Could not persist Directory discovery cache")
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not clean temporary Directory discovery cache")


def _prune_cache(config: DirectoryCatalogConfig) -> None:
    """Bound generated discovery cache identities without touching unknown files."""
    if config.cache_dir is None or not config.cache_dir.is_dir():
        return
    known = []
    current = _cache_path(config)
    for path in config.cache_dir.iterdir():
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[a-f0-9]{20}\.(?:json|tmp)", path.name):
            continue
        state = path.stat()
        age = time.time() - state.st_mtime
        if state.st_size > MAX_CACHE_BYTES or age > config.cache_lkg or (
            path.suffix == ".tmp" and age > 3600
        ):
            path.unlink()
        elif path.suffix == ".json":
            known.append((path == current, state.st_mtime_ns, path))
    for _, _, path in sorted(known, reverse=True)[MAX_CACHE_FILES:]:
        path.unlink()


def load_directory_catalog_channels(
    config: DirectoryCatalogConfig,
    default_resolution: str = "best",
    session: requests.Session | None = None,
    continue_on_error: bool = True,
) -> list[Channel]:
    try:
        _prune_cache(config)
    except OSError:
        logger.warning("Could not prune Directory discovery cache")
    cached = _read_cache(config)
    if cached and cached[1] <= config.cache_ttl:
        return cached[0]
    owns_session = session is None
    session = session or requests.Session()
    try:
        # Use the site's ordinary browsing flow on this same Session. A failed
        # isolated iframe GET is not evidence that the channel is unavailable.
        _get_text(session, BASE_URL, config.timeout, headers=_page_headers())
        params = {"exclude": ",".join(config.exclude_slugs)} if config.exclude_slugs else {}
        payload = json.loads(_get_text(
            session, INDEX_URL, config.timeout, params=params,
            headers=_page_headers(BASE_URL, ajax=True),
        ))
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or not isinstance(payload.get("channels"), list)
        ):
            raise ValueError("Invalid Directory channel index")
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
                id="directory_catalog." + slug,
                name=str(item.get("name") or slug),
                source_url=page_url,
                logo=_same_origin_url(item.get("logo")) or "",
                group=str(item.get("category") or "general"),
                source_type=SOURCE_TYPE,
                provider_id=SOURCE_TYPE,
                logical_channel_id="directory_catalog." + slug,
                variant_id="directory_catalog." + slug,
                resolution=default_resolution,
                requires_dynamic_resolution=True,
                publishable_static=False,
                delivery_mode="embed",
            )
            try:
                _get_text(session, page_url, config.timeout, headers=_page_headers(BASE_URL))
                details = json.loads(_get_text(
                    session, ajax_url, config.timeout,
                    params={"tz": "America/Sao_Paulo"},
                    headers=_page_headers(page_url, ajax=True),
                ))
                if details.get("ok") is not True or details.get("slug") != slug:
                    raise ValueError("Invalid Directory player metadata")
                iframe_url = _same_origin_url(details.get("iframe"))
                if iframe_url:
                    player = _get_text(
                        session, _autoplay_url(iframe_url), config.timeout,
                        headers=_page_headers(page_url),
                    )
                    per_slug_hosts = dict(config.reviewed_hosts_by_slug)
                    channel_config = replace(config, reviewed_stream_hosts=per_slug_hosts[slug]) if slug in per_slug_hosts else config
                    _apply_player(channel, player, channel_config)
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                logger.warning("Directory player unavailable slug=%s error_type=%s", slug, type(exc).__name__)
            channels.append(channel)
        if not channels:
            raise ValueError("No reviewed Directory channels found")
        _write_cache(config, channels)
        return channels
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logger.warning("Directory discovery failed error_type=%s", type(exc).__name__)
        if cached:
            return cached[0]
        if not continue_on_error:
            raise
        return []
    finally:
        if owns_session:
            session.close()


def load_configured_directory_catalog(default_resolution: str = "best") -> list[Channel]:
    config = config_from_environment()
    if config is None:
        return []
    return load_directory_catalog_channels(config, default_resolution=default_resolution)
