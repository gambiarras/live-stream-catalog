# Televisão.TV discovery

The adapter is an optional, bounded discovery source for a reviewed selection of
TV channels. It does not assume the site is a media API or a redistribution
licence. It is disabled unless `LIVE_TELEVISAO_SLUGS` is set.

## Observed public contract

Investigation date: 2026-11-14. The public
[`channels-data.php`](https://televisao.tv/channels-data.php) endpoint returned
an object containing `ok`, `count` and `channels`. The 371 channel records had
`slug`, `name`, `category`, `categories`, `abbr`, `color`, `logo`, `url` and
`ajaxUrl`. Image and page references were relative to `https://televisao.tv/`.
These records contained **no direct media URL**.

`exclude` is a comma-separated list of channel slugs to omit from the response.
The request `?exclude=sbt,tv-brasil` returned 369 entries, removing exactly those
two slugs; an unknown slug left the 371-entry set unchanged. It is a membership
filter, not an access credential, paid-channel classifier or quality selector.
The longer example supplied by the user removes its listed slugs in the same
way. The importer also enforces exclusions locally.

For selected channels, `ajaxUrl` returns player metadata including `iframe`.
The iframe is still HTML, not automatically a playable URL. On the investigation
date the site announced these paths, which all returned HTTP 404:

- `/iframes/hls-player.php?ch=retro-cartoon`
- `/iframes/hls-player.php?ch=tv-cultura`
- `/iframes/youtube_player.php?ch=tv-brasil`
- `/iframe/agrobrasil.html`

The Retro Cartoon page embedded the same unavailable iframe. The importer
preserves such entries as pending, non-publishable embed candidates, without
substituting the HTML page as a stream. Fox Kids did not appear in the index;
the existing Fox Kids and Retrô Cartoon extra sources are independent.

## Configuration

| Variable | Meaning |
| --- | --- |
| `LIVE_TELEVISAO_SLUGS` | Explicit reviewed slugs, separated by commas; 1–24 channels per run. Empty disables discovery. |
| `LIVE_TELEVISAO_EXCLUDE` | Optional channel slugs to omit. |
| `LIVE_TELEVISAO_REVIEWED_STREAM_HOSTS` | Exact original media hostnames reviewed by the operator. Empty permits discovery only, never direct-media publication. |
| `LIVE_TELEVISAO_CACHE_DIR` | Discovery cache directory, default `.cache/televisao`; empty disables disk cache. |
| `LIVE_TELEVISAO_TIMEOUT` | Timeout per request in seconds, default 15. |

The public index is fetched once, followed by at most two requests per selected
channel (AJAX metadata and one same-origin iframe). Responses are capped at
2 MB of actual decoded bytes. Requests are sequential; redirects, nested
third-party iframe traversal, executable JavaScript and security challenges are
not followed or executed. Valid discovery is cached for six hours; an index
failure may reuse the last valid discovery for at most 24 hours. Cache identity
includes the reviewed slugs, exclusions and media hosts. Cache writes are atomic
and use sanitized public channel serialization.

Example using only this source, without invoking all other catalog providers:

```python
from pathlib import Path
from live_stream_catalog.sources.televisao_catalog import (
    TelevisaoCatalogConfig,
    load_televisao_channels,
)

channels = load_televisao_channels(
    TelevisaoCatalogConfig(
        slugs=("retro-cartoon",),
        cache_dir=Path(".cache/televisao"),
    )
)
```

An empty reviewed-host set in this example is intentional. It lets the pilot
discover availability/capabilities without approving arbitrary original media
hosts merely because they appear in an aggregator.

## Media and provenance

Direct HLS/DASH/MP4 endpoints are accepted only as literal URLs in declared
player configuration, on explicitly reviewed media hosts, without recognized
credentials or expiry. A declared transport can have `status=resolved`; this
does not mean video playback was validated. Per-source validation remains a
separate step.

IDs are stable `televisao.<slug>` values; `provider_id=televisao`, the category
and canonical page/logo remain associated with the channel. YouTube iframe
videos become platform-resolution candidates with the YouTube page as
`source_url`, retaining the Televisão provider and logical identity. They remain
dynamic and non-publishable: neither a YouTube page nor a signed media URL is
exported as a supposedly permanent IPTV stream.

An IPTV player needs direct supported media or its own platform resolver for
YouTube. The catalog cannot make a browser page playable in Media3 merely by
placing it into an M3U. This change does not relax the existing publication
guards for YouTube, authentication, DRM or expiring transport URLs.

## Terms and limitations

The site's [terms](https://televisao.tv/termos-de-uso) prohibit abusive
automation, bulk copying and bypassing security measures. They also explain
that streams/players can belong to third parties. The
[copyright policy](https://televisao.tv/dmca) does not establish a licence to
redistribute every listed stream. The pilot therefore does not crawl all 371
players or copy editorial articles. Operators must review origin permissions
for their selected channels and must not use a matching name in this directory
as proof that a pay-TV source is authorized.

Current 404 results mean this source adds useful discovery metadata but did not
supply a new playable transport for the tested slugs. Do not fabricate streams
or treat absent directory entries as proof that a broadcaster is paid.

Tests use synthetic/sanitized responses and cover disabled defaults, bounded
selection, membership exclusions, provenance, missing players, reviewed hosts,
credential/expiry guards, YouTube capabilities, cross-origin rejection,
response-size limits, cache expiry and last-known-good behavior.
