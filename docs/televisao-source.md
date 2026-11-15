# Televisão.TV discovery

The adapter is an optional, bounded discovery source for a reviewed selection of
TV channels. It does not assume the site is a media API or a redistribution
licence. Its default is six public broadcasters: Canal Gov, TV Cultura, TV Brasil,
TV Senado, TV Câmara and TV Justiça. Set `LIVE_TELEVISAO_SLUGS=` explicitly to
disable discovery. The versioned review is in
[`televisao_defaults.json`](../live_stream_catalog/resources/televisao_defaults.json).

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
date four isolated direct requests to the following declared paths returned
HTTP 404. This was a limited sample of requests, **not a finding that all 371
channels or their underlying streams were unavailable**:

- `/iframes/hls-player.php?ch=retro-cartoon`
- `/iframes/hls-player.php?ch=tv-cultura`
- `/iframes/youtube_player.php?ch=tv-brasil`
- `/iframe/agrobrasil.html`

The Retro Cartoon page embedded the same iframe reference. Fox Kids did not
appear in the index; the existing Fox Kids and Retrô Cartoon extra sources are
independent.

Recheck on 2026-11-15 followed the ordinary site flow in one HTTP session:
home, channel page, AJAX metadata and the declared iframe, using page-appropriate
Accept, User-Agent and Referer headers. All six tested players (the four above,
SBT and Band) returned HTML 200. The user's Canal Gov and RedeTV examples also
returned 200 in that flow. Retro Cartoon, TV Cultura and Canal Gov declared HLS
endpoints; RedeTV embedded Dailymotion, Agrobrasil embedded the broadcaster's
player, and some YouTube-player wrappers contained another same-origin iframe.
The adapter does not traverse these additional embeds by default.

Isolated requests remained 404 in the recheck, even with individual or combined
headers. The exact server-side cause was not isolated; do not attribute it to
one header or claim that `autoplay=1` alone fixes it. HTML 200 confirms a player
document was obtained, not that video playback works. The adapter now follows
the observed normal browsing flow and retains its publication/capability guards.

## Curated default and scope

The 2026-11-15 pilot returned six candidates and two direct-media candidates.
Only Canal Gov (`canalgov-stream.ebc.com.br`) and TV Cultura
(`player-tvcultura.stream.uol.com.br`) have reviewed literal media hosts in the
default. The review links each hostname to its own public broadcaster; the other
four defaults remain discovery-only until a supported authorized player is
identified. Official public live pages: [Canal Gov](https://canalgov.ebc.com.br/)
and [TV Cultura](https://cultura.uol.com.br/aovivo/). Public viewing does not
authorize redistribution or promise playback on every device.

Provider provenance is retained even when another provider has the same media
URL. Different request-header contexts from the same provider are also kept;
header names are normalized without discarding Referer/User-Agent differences.
This lets the consumer prefer Televisão.TV and then Batista Play, IPTV-org,
YouTube and extras. FrostView configurations are ignored before HTTP/cache
loading, including stale environment overrides; other add-ons default disabled.

## Configuration

| Variable | Meaning |
| --- | --- |
| `LIVE_TELEVISAO_SLUGS` | Six curated defaults, or explicit reviewed slugs separated by commas; 1–24 channels per run. Explicit empty disables discovery. |
| `LIVE_TELEVISAO_EXCLUDE` | Optional channel slugs to omit. |
| `LIVE_TELEVISAO_REVIEWED_STREAM_HOSTS` | Exact original media hostnames reviewed by the operator. Empty permits discovery only, never direct-media publication. |
| `LIVE_TELEVISAO_CACHE_DIR` | Discovery cache directory, default `.cache/televisao`; empty disables disk cache. |
| `LIVE_TELEVISAO_TIMEOUT` | Timeout per request in seconds, default 15. |

The homepage and public index are fetched once, followed by at most three
requests per selected channel (page, AJAX metadata and one same-origin iframe).
AJAX receives the site's time-zone parameter and request header; the iframe
receives the observed autoplay parameter. Responses are capped at
2 MB of actual decoded bytes. Requests are sequential; redirects, nested
third-party iframe traversal, executable JavaScript and security challenges are
not followed or executed. Valid discovery is cached for six hours; an index
failure may reuse the last valid discovery for at most 24 hours. Generated cache
files are capped at 8 MB each and four fingerprints, with expired files and
interrupted temporary writes removed; unknown operator files are preserved. Cache identity
includes the request-protocol version, reviewed slugs, exclusions and media
hosts. Version 2 invalidates candidates cached by the earlier isolated-request
protocol. Cache writes are atomic
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

The original 404 sample did not establish that the directory was unusable.
The recheck found player documents with declared direct media and other player
types; their transport and client compatibility must still be checked
individually. Do not fabricate streams, mistake an embed for direct media or
treat absent directory entries as proof that a broadcaster is paid.

Tests use synthetic/sanitized responses and cover disabled defaults, bounded
selection, membership exclusions, provenance, missing players, reviewed hosts,
credential/expiry guards, YouTube capabilities, cross-origin rejection,
response-size limits, cache expiry and last-known-good behavior.
