import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import requests

from live_stream_catalog.services.resolver import resolve_channel
from live_stream_catalog.sources.televisao_catalog import (
    BASE_URL,
    INDEX_URL,
    MAX_RESPONSE_BYTES,
    TelevisaoCatalogConfig,
    config_from_environment,
    load_configured_televisao_catalog,
    load_televisao_channels,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "televisao_channels.json"


class Response:
    def __init__(self, content, status_code=200):
        self.content = content.encode() if isinstance(content, str) else content
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("unavailable")

    def iter_content(self, size):
        for start in range(0, len(self.content), size):
            yield self.content[start : start + size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, player_html=None):
        self.calls = []
        self.responses = []
        self.player_html = player_html or '<video><source src="https://media.example.test/live.m3u8"></video>'

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == INDEX_URL:
            response = Response(FIXTURE.read_text())
        elif "ajax.php" in url:
            slug = url.rsplit("=", 1)[-1]
            response = Response(json.dumps({"ok": True, "slug": slug, "iframe": "/player/" + slug}))
        elif url.endswith("/unavailable-tv"):
            response = Response("missing", 404)
        elif url.endswith("/youtube-tv"):
            response = Response('<iframe src="https://www.youtube.com/embed/Abcde12345_"></iframe>')
        else:
            response = Response(self.player_html)
        self.responses.append(response)
        return response


class TelevisaoCatalogTest(unittest.TestCase):
    def config(self, **kwargs):
        return TelevisaoCatalogConfig(
            slugs=("public-tv",),
            reviewed_stream_hosts=("media.example.test",),
            **kwargs,
        )

    def test_disabled_without_explicit_reviewed_slugs(self):
        with patch.dict("os.environ", {}, clear=True), patch("requests.Session") as session:
            self.assertIsNone(config_from_environment())
            self.assertEqual(load_configured_televisao_catalog(), [])
        session.assert_not_called()

    def test_validates_bounded_slug_configuration(self):
        for slugs in [(), ("../unsafe",), tuple("tv-" + str(i) for i in range(25))]:
            with self.subTest(slugs=slugs), self.assertRaises(ValueError):
                TelevisaoCatalogConfig(slugs=slugs)

    def test_loads_only_reviewed_players_and_preserves_provenance(self):
        session = Session()
        result = load_televisao_channels(self.config(), session=session)
        self.assertEqual(len(result), 1)
        channel = result[0]
        self.assertEqual(channel.id, "televisao.public-tv")
        self.assertEqual(channel.logical_channel_id, channel.id)
        self.assertEqual(channel.source_url, BASE_URL + "public-tv")
        self.assertEqual(channel.logo, BASE_URL + "images/public.webp")
        self.assertEqual(channel.provider_id, "televisao")
        self.assertEqual(channel.group, "culture")
        self.assertEqual(channel.protocol, "hls")
        self.assertTrue(channel.publishable_static)
        self.assertFalse(channel.requires_dynamic_resolution)
        self.assertEqual(channel.status, "resolved")
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(all(response.closed for response in session.responses))
        with patch("live_stream_catalog.services.resolver.build_streamlink_session") as streamlink:
            resolved = resolve_channel(channel)
        streamlink.assert_not_called()
        self.assertIsNotNone(resolved.resolved_at)

    def test_exclude_is_sent_as_slug_filter_and_enforced_locally(self):
        session = Session()
        config = replace(self.config(), exclude_slugs=("public-tv",))
        result = load_televisao_channels(config, session=session)
        self.assertEqual(result, [])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]["params"], {"exclude": "public-tv"})

    def test_missing_player_is_nonpublishable_and_not_re_resolved_as_media(self):
        session = Session()
        result = load_televisao_channels(replace(self.config(), slugs=("unavailable-tv",)), session=session)
        channel = result[0]
        self.assertEqual(channel.status, "pending")
        self.assertEqual(channel.delivery_mode, "embed")
        self.assertFalse(channel.publishable_static)
        self.assertIsNone(channel.stream_url)
        with patch("live_stream_catalog.services.resolver.build_streamlink_session") as streamlink:
            self.assertIs(resolve_channel(channel), channel)
        streamlink.assert_not_called()

    def test_youtube_embed_keeps_page_dynamic_and_omits_public_media(self):
        result = load_televisao_channels(replace(self.config(), slugs=("youtube-tv",)), session=Session())
        channel = result[0]
        self.assertEqual(channel.source_type, "youtube")
        self.assertEqual(channel.provider_id, "televisao")
        self.assertEqual(channel.source_url, "https://www.youtube.com/watch?v=Abcde12345_")
        self.assertTrue(channel.requires_dynamic_resolution)
        self.assertFalse(channel.publishable_static)
        self.assertIsNone(channel.to_dict()["stream_url"])

    def test_signed_unreviewed_and_non_media_urls_are_not_exportable(self):
        for html in [
            '<source src="https://media.example.test/live.m3u8?token=private">',
            '<source src="https://media.example.test/live.m3u8?username=account&amp;password=private">',
            '<source src="https://media.example.test/live.m3u8?apikey=private">',
            '<source src="https://media.example.test/live.m3u8?expire=1">',
            '<source src="https://media.example.test/live.m3u8?EXPIRES=1794680000">',
            '<source src="https://unknown.example.test/live.m3u8">',
            '<source src="https://media.example.test/a-page.html">',
            '<source src="http://media.example.test/a-page.html">',
            '<iframe src="https://external.example.test/player"></iframe>',
        ]:
            with self.subTest(html=html):
                channel = load_televisao_channels(self.config(), session=Session(html))[0]
                self.assertFalse(channel.publishable_static)
                self.assertIsNone(channel.stream_url)
                self.assertNotIn("private", json.dumps(channel.to_dict()))

    def test_literal_javascript_hls_is_read_without_executing_code(self):
        player = 'hls.loadSource("https:\\/\\/media.example.test/live.m3u8");'
        channel = load_televisao_channels(self.config(), session=Session(player))[0]
        self.assertEqual(channel.stream_url, "https://media.example.test/live.m3u8")

    def test_rejects_cross_origin_metadata(self):
        result = load_televisao_channels(replace(self.config(), slugs=("external-tv",)), session=Session())
        self.assertEqual(result, [])

    def test_cache_ttl_last_good_and_expiration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(cache_dir=Path(directory), cache_ttl=60, cache_lkg=120)
            with patch("live_stream_catalog.sources.televisao_catalog.time.time", return_value=1000):
                original = load_televisao_channels(config, session=Session())
            offline = Session()
            offline.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError())
            with patch("live_stream_catalog.sources.televisao_catalog.time.time", return_value=1030):
                fresh = load_televisao_channels(config, session=offline)
            with patch("live_stream_catalog.sources.televisao_catalog.time.time", return_value=1090):
                fallback = load_televisao_channels(config, session=offline)
            with patch("live_stream_catalog.sources.televisao_catalog.time.time", return_value=1130):
                expired = load_televisao_channels(config, session=offline)
            self.assertEqual(fresh[0].stream_url, original[0].stream_url)
            self.assertEqual(fallback[0].stream_url, original[0].stream_url)
            self.assertEqual(expired, [])

    def test_cache_changes_with_reviewed_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(cache_dir=Path(directory))
            load_televisao_channels(config, session=Session())
            unreviewed = replace(config, reviewed_stream_hosts=())
            channel = load_televisao_channels(unreviewed, session=Session())[0]
            self.assertFalse(channel.publishable_static)

    def test_index_failure_does_not_hide_error_in_strict_mode(self):
        session = Session()
        session.get = lambda *_args, **_kwargs: Response("<html>failure</html>")
        with self.assertRaises(ValueError):
            load_televisao_channels(self.config(), session=session, continue_on_error=False)

    def test_caps_body_and_closes_response(self):
        session = Session()
        response = Response(b"x" * (MAX_RESPONSE_BYTES + 1))
        session.get = lambda *_args, **_kwargs: response
        with self.assertRaises(ValueError):
            load_televisao_channels(self.config(), session=session, continue_on_error=False)
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
