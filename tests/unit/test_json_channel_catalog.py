import unittest
from unittest.mock import patch

from live_stream_catalog.sources.json_channel_catalog import (
    JsonChannelCatalogConfig,
    load_config_resource,
    load_json_catalog_channels,
)


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "channels": [
                {
                    "id": "globo-rj",
                    "name": "Globo RJ",
                    "category": "TV Aberta",
                    "tvg_id": "TVGloboRiodeJaneiro.br",
                    "variants": [
                        {
                            "id": "fhd",
                            "quality": "1080p",
                            "url": "https://media.example.test/globo-fhd.m3u8",
                        },
                        {
                            "id": "4k",
                            "quality": "2160p",
                            "codec": "hevc",
                            "url": "https://media.example.test/globo-4k.m3u8",
                            "drm": {"type": "widevine"},
                            "publishable_static": "true",
                        },
                    ],
                },
                {
                    "id": "adult",
                    "name": "Adult Channel",
                    "category": "+18",
                    "url": "https://media.example.test/adult.m3u8",
                },
            ]
        }


class FakeSession:
    def get(self, url, headers=None, timeout=None):
        self.url = url
        self.headers = headers
        return FakeResponse()


class JsonChannelCatalogTest(unittest.TestCase):
    def test_loads_endpoint_and_runtime_headers_from_environment(self):
        with patch.dict(
            "os.environ",
            {
                "LIVE_JSON_CATALOG_URL": "https://api.example.test/channels",
                "LIVE_JSON_CATALOG_HEADERS": '{"Authorization":"Bearer runtime"}',
            },
        ):
            configs = load_config_resource()

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].provider_id, "json_catalog_1")
        self.assertEqual(configs[0].request_headers, {"Authorization": "Bearer runtime"})

    def test_keeps_provider_categories_and_quality_variants(self):
        config = JsonChannelCatalogConfig(
            id="json_catalog_1",
            provider_id="json_catalog_1",
            endpoint_url="https://api.example.test/channels",
            request_headers={"X-Client": "test"},
        )

        session = FakeSession()
        channels = load_json_catalog_channels([config], session=session)

        self.assertEqual(len(channels), 2)
        self.assertEqual({channel.group for channel in channels}, {"TV Aberta"})
        self.assertEqual({channel.name for channel in channels}, {"Globo RJ [FHD]", "Globo RJ [4K] [HEVC]"})
        self.assertEqual(len({channel.logical_channel_id for channel in channels}), 1)
        self.assertEqual({channel.tvg_id for channel in channels}, {"TVGloboRiodeJaneiro.br"})
        self.assertEqual(session.headers["X-Client"], "test")
        self.assertFalse(next(channel for channel in channels if "4K" in channel.name).publishable_static)
        self.assertNotIn("api.example.test", str([channel.to_dict() for channel in channels]))


if __name__ == "__main__":
    unittest.main()
