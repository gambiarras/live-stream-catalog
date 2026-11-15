import unittest

from live_stream_catalog.models import Channel
from live_stream_catalog.sources.loader import _deduplicate


class LoaderTest(unittest.TestCase):
    def test_preserves_same_id_with_different_source_urls(self):
        channels = [
            Channel(
                id="same.tv",
                name="Primary",
                source_url="https://example.test/primary",
                logo="",
                group="general",
                source_type="youtube",
            ),
            Channel(
                id="same.tv",
                name="Alternative",
                source_url="https://example.test/alternative",
                logo="",
                group="general",
                source_type="youtube",
            ),
        ]

        result = _deduplicate(channels)

        self.assertEqual(result, channels)

    def test_removes_duplicate_source_urls(self):
        channels = [
            Channel(
                id="first.tv",
                name="First",
                source_url="https://example.test/live",
                logo="",
                group="general",
                source_type="youtube",
            ),
            Channel(
                id="second.tv",
                name="Second",
                source_url="https://example.test/live",
                logo="",
                group="general",
                source_type="youtube",
            ),
        ]

        with self.assertLogs("live_stream_catalog.sources.loader", level="WARNING"):
            result = _deduplicate(channels)

        self.assertEqual(result, [channels[0]])

    def test_keeps_variants_with_same_source_page_and_different_stream_urls(self):
        channels = [
            Channel(
                id="globo.fhd",
                name="Globo [FHD]",
                source_url="https://provider.example.test/globo",
                stream_url="https://media.example.test/globo-fhd.m3u8",
                logo="",
                group="TV Aberta",
                source_type="stremio_addon",
                provider_id="addon_catalog_1",
                logical_channel_id="globo",
                variant_id="globo.fhd",
            ),
            Channel(
                id="globo.4k",
                name="Globo [4K]",
                source_url="https://provider.example.test/globo",
                stream_url="https://media.example.test/globo-4k.m3u8",
                logo="",
                group="TV Aberta",
                source_type="stremio_addon",
                provider_id="addon_catalog_1",
                logical_channel_id="globo",
                variant_id="globo.4k",
            ),
        ]

        self.assertEqual(_deduplicate(channels), channels)

    def test_preserves_cross_provider_transport_for_downstream_priority(self):
        channels = [Channel(id=provider, name="Public TV", source_url="https://example.test/page",
                            stream_url="https://media.example.test/live.m3u8", logo="", group="TV Aberta",
                            source_type=provider, provider_id=provider)
                    for provider in ("rest_catalog_1", "televisao")]
        self.assertEqual(_deduplicate(channels), channels)

    def test_preserves_header_contexts_and_normalizes_header_names(self):
        channels = [Channel(id=str(index), name="Public TV", source_url="https://example.test/page",
                            stream_url="https://media.example.test/live.m3u8", logo="", group="TV Aberta",
                            source_type="rest", provider_id="same-provider", request_headers=headers)
                    for index, headers in enumerate([
                        {"Referer": "https://one.example.test/", "User-Agent": "Player"},
                        {"Referer": "https://two.example.test/", "User-Agent": "Player"},
                        {"user-agent": "Player", "referer": "https://one.example.test/"},
                    ])]
        with self.assertLogs("live_stream_catalog.sources.loader", level="WARNING"):
            self.assertEqual(_deduplicate(channels), channels[:2])


if __name__ == "__main__":
    unittest.main()
