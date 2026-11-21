from tests import fixture_config  # noqa: F401

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from live_stream_catalog.models import Channel
from live_stream_catalog.services.resolver import resolve_channel
from live_stream_catalog.video_platform_transport import is_public_video_platform_hls, media_hls_expiry


NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
EXPIRY = int((NOW + timedelta(hours=4)).timestamp())
MEDIA = f"https://manifest.media.example.test/api/manifest/hls_variant/expire/{EXPIRY}/sig/public-media"


def channel(**values):
    base = dict(id="sample.abcdefghijk", name="Sample", logo="", group="general",
                source_type="video_platform", source_url="https://video.example.test/watch?v=abcdefghijk",
                stream_url=MEDIA, status="resolved", protocol="hls",
                requires_dynamic_resolution=True, publishable_static=False,
                resolved_at=(NOW - timedelta(minutes=15)).isoformat(),
                expires_at=(NOW + timedelta(hours=4)).isoformat())
    base.update(values)
    return Channel(**base)


class VideoPlatformTransportTest(unittest.TestCase):
    def test_accepts_public_resolved_hls_and_retains_truthful_capability_flags(self):
        item = channel()
        self.assertTrue(is_public_video_platform_hls(item, now=NOW))
        with patch("live_stream_catalog.video_platform_transport.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            payload = item.to_dict()
        self.assertEqual(payload["stream_url"], MEDIA)
        self.assertEqual(payload["source_url"], item.source_url)
        self.assertTrue(payload["requires_dynamic_resolution"])
        self.assertFalse(payload["publishable_static"])
        self.assertEqual(payload["resolved_at"], item.resolved_at)

    def test_accepts_scoped_media_signatures_and_public_live_source_shapes(self):
        for source in ("https://video-short.example.test/abcdefghijk", "https://video.example.test/live/abcdefghijk",
                       "https://video.example.test/@sample/live", "https://video.example.test/channel/UCsample/live",
                       "https://video.example.test/c/sample/live", "https://video.example.test/user/sample/live"):
            with self.subTest(source=source):
                item = channel(source_url=source,
                               stream_url=f"https://r1.media.example.test/live.m3u8?expire={EXPIRY}&sig=media&signature=media")
                self.assertTrue(is_public_video_platform_hls(item, now=NOW))

    def test_rejects_credentials_even_when_values_are_empty_or_encoded(self):
        for suffix in ("&token=", "&cookie=secret", "&%70assword=secret", "&session=private",
                       "&username=account", "&Authorization=Bearer", "&x-amz-signature=private"):
            with self.subTest(suffix=suffix):
                self.assertFalse(is_public_video_platform_hls(channel(
                    stream_url=f"https://r1.media.example.test/live.m3u8?expire={EXPIRY}{suffix}"), now=NOW))
        self.assertFalse(is_public_video_platform_hls(channel(
            stream_url=f"https://r1.media.example.test/api/manifest/hls_playlist/expire/{EXPIRY}/token/private"), now=NOW))

    def test_rejects_non_media_hosts_authority_tricks_and_non_hls_urls(self):
        urls = (f"https://evilmedia.example.test/live.m3u8?expire={EXPIRY}",
                f"https://media.example.test.evil.test/live.m3u8?expire={EXPIRY}",
                f"https://user:password@r1.media.example.test/live.m3u8?expire={EXPIRY}",
                f"http://r1.media.example.test/live.m3u8?expire={EXPIRY}",
                f"https://r1.media.example.test:8080/live.m3u8?expire={EXPIRY}",
                f"https://r1.media.example.test/live.mpd?expire={EXPIRY}",
                f"https://r1.media.example.test/videoplayback?expire={EXPIRY}",
                f"https://r1.media.example.test/live.m3u8?expire={EXPIRY}#token",
                f"https://r1.media.example.test/live.m3u8?expire={EXPIRY}\n")
        for url in urls:
            with self.subTest(url=url):
                self.assertFalse(is_public_video_platform_hls(channel(stream_url=url), now=NOW))

    def test_rejects_non_public_source_pages_and_signed_source_links(self):
        for source in ("https://evil.video.example.test/watch?v=abcdefghijk",
                       "https://video.example.test/watch?v=short", "https://video.example.test/watch?v=abcdefghijk&v=",
                       "https://video.example.test/watch?v=abcdefghijk&token=", "https://video.example.test/playlist?list=public",
                       "https://video-short.example.test/abcdefghijk?sig=private", "https://video.example.test/@sample/live?signature=private",
                       "http://video.example.test/watch?v=abcdefghijk", "https://video.example.test/watch?v=abcdefghijk#part"):
            with self.subTest(source=source):
                self.assertFalse(is_public_video_platform_hls(channel(source_url=source), now=NOW))

    def test_requires_real_expiry_and_uses_earliest_value_and_advertised_expiry(self):
        for seconds, eligible in ((-1, False), (899, False), (900, True), (901, True)):
            with self.subTest(seconds=seconds):
                expiry = int((NOW + timedelta(seconds=seconds)).timestamp())
                item = channel(stream_url=f"https://r1.media.example.test/live.m3u8?expire={expiry}")
                self.assertEqual(is_public_video_platform_hls(item, now=NOW), eligible)
        for item in (channel(stream_url="https://r1.media.example.test/live.m3u8"),
                     channel(stream_url="https://r1.media.example.test/live.m3u8?expire=invalid"),
                     channel(stream_url=f"https://r1.media.example.test/live.m3u8?expire={EXPIRY}&expire=1"),
                     channel(stream_url=f"https://r1.media.example.test/live.m3u8?expire={EXPIRY}&expire="),
                     channel(stream_url=f"https://r1.media.example.test/live.m3u8?expire={10**40}"),
                     channel(expires_at=(NOW + timedelta(seconds=899)).isoformat()),
                     channel(expires_at="2026-11-21T15:00:00")):
            self.assertFalse(is_public_video_platform_hls(item, now=NOW))
        self.assertEqual(media_hls_expiry(MEDIA), NOW + timedelta(hours=4))

    def test_preserves_all_private_transport_blockers_and_other_dynamic_redaction(self):
        restrictions = (dict(source_type="game_platform"), dict(status="pending"), dict(status="error"),
                        dict(removed=True), dict(drm={}), dict(secret_refs={"cookies": "PRIVATE_COOKIES"}),
                        dict(delivery_mode="ssai"), dict(delivery_mode="dai"), dict(protocol="dash"),
                        dict(request_headers={"Cookie": "secret"}), dict(request_headers={"Authorization": "secret"}))
        for values in restrictions:
            with self.subTest(values=values):
                self.assertFalse(is_public_video_platform_hls(channel(**values), now=NOW))
        item = channel(source_type="json_catalog", stream_url="https://public.example/live.m3u8")
        self.assertIsNone(item.to_dict()["stream_url"])

    def test_sanitizing_private_import_headers_cannot_make_a_video_platform_url_public(self):
        item = channel()
        with patch("live_stream_catalog.video_platform_transport.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            payload = item.to_dict()
            for private in (dict(request_headers={"Cookie": "private"}),
                            dict(request_headers={"Authorization": "private"}),
                            dict(request_headers="Cookie: private"),
                            dict(secret_refs={"invalid": "not-an-environment-name"})):
                imported = Channel.from_dict({**payload, **private})
                self.assertIsNone(imported.stream_url)
                self.assertFalse(is_public_video_platform_hls(imported, now=NOW))

    def test_resolver_recognizes_extensionless_media_host_hls_without_changing_dynamic_flags(self):
        item = channel(status="pending", stream_url=None, protocol=None, expires_at=None)
        session = Mock()
        session.streams.return_value = {"best": Mock(url=MEDIA, multivariant=None)}
        with patch("live_stream_catalog.services.resolver.build_streamlink_session", return_value=session), \
             patch("live_stream_catalog.services.resolver.utc_now", return_value=NOW), \
             patch("live_stream_catalog.services.expiry.utc_now", return_value=NOW), \
             patch("live_stream_catalog.video_platform_transport.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            resolved = resolve_channel(item)
            payload = resolved.to_dict()
        self.assertEqual(resolved.protocol, "hls")
        self.assertTrue(resolved.requires_dynamic_resolution)
        self.assertFalse(resolved.publishable_static)
        self.assertEqual(payload["stream_url"], MEDIA)
        self.assertEqual(payload["expires_at"], (NOW + timedelta(hours=4)).isoformat())

    def test_private_configuration_controls_hosts_paths_and_expiry_without_provider_defaults(self):
        settings = {
            "page_hosts": ["portal.example.test"], "short_hosts": ["clip.example.test"],
            "media_hosts": ["delivery.example.test"],
            "watch_url": "https://portal.example.test/play?id={video_id}",
            "video_id_query": "id", "source_path_patterns": [r"/station-[a-z]+/live"],
            "media_path_patterns": [r"^/renewable-hls/"],
            "expiry_fields": ["deadline"], "expiry_path_field": "until",
        }
        item = channel(source_url="https://portal.example.test/play?id=abcdefghijk",
                       stream_url=f"https://r1.delivery.example.test/renewable-hls/until/{EXPIRY}")
        with patch("live_stream_catalog.video_platform_transport.source_settings", return_value=settings):
            self.assertTrue(is_public_video_platform_hls(item, now=NOW))
            item.source_url = "https://portal.example.test/station-sample/live"
            item.stream_url = f"https://r1.delivery.example.test/renewable-hls/master?deadline={EXPIRY}"
            self.assertTrue(is_public_video_platform_hls(item, now=NOW))
            item.source_url = "https://clip.example.test/abcdefghijk"
            self.assertTrue(is_public_video_platform_hls(item, now=NOW))
            item.stream_url = f"https://delivery.example.test.evil.test/renewable-hls/master?deadline={EXPIRY}"
            self.assertFalse(is_public_video_platform_hls(item, now=NOW))
        with patch("live_stream_catalog.video_platform_transport.source_settings", return_value={}):
            self.assertFalse(is_public_video_platform_hls(channel(), now=NOW))

    def test_remaining_ttl_field_cannot_override_real_expiry_or_accept_nonfinite_values(self):
        for ttl in (899, -1, "bad", "900", True, float("nan"), float("inf"), 900.5):
            with self.subTest(ttl=ttl):
                self.assertFalse(is_public_video_platform_hls(channel(ttl_seconds=ttl), now=NOW))
        for ttl in (900, 900.0):
            with self.subTest(ttl=ttl):
                self.assertTrue(is_public_video_platform_hls(channel(ttl_seconds=ttl), now=NOW))
        self.assertFalse(is_public_video_platform_hls(channel(expires_at=123), now=NOW))


if __name__ == "__main__":
    unittest.main()
