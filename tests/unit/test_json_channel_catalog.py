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


class LinkedFakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class LinkedFakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if len(self.calls) == 1:
            return LinkedFakeResponse({"links": ["entry-a", "entry-b"]})
        return LinkedFakeResponse(
            {
                "entries": [
                    {
                        "_meta": {"id": "entry-a"},
                        "title": "Primary channel",
                        "ovpId": "epg-a",
                        "ssaiUrl": "https://media.example.test/live.m3u8?w=[WIDTH]",
                    },
                    {
                        "_meta": {"id": "entry-b"},
                        "title": "Encrypted channel",
                        "ovpId": "epg-b",
                        "ssaiUrl": "https://media.example.test/drm.m3u8",
                    },
                ]
            }
        )


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

    def test_loads_linked_catalog_and_keeps_dynamic_delivery_private(self):
        config = JsonChannelCatalogConfig(
            id="json_catalog_1",
            provider_id="json_catalog_1",
            endpoint_url="https://api.example.test/index",
            request_headers={},
            linked_entries_url="https://api.example.test/entries",
            linked_ids_path="links",
            linked_query_param="id",
            linked_query_params={"offset": "0", "size": "50"},
            items_path="entries",
            item_id_path="ovpId",
            stream_url_path="ssaiUrl",
            tvg_id_path="ovpId",
            default_delivery_mode="ssai",
            default_requires_dynamic_resolution=True,
            default_publishable_static=False,
            item_overrides={
                "epg-a": {"name": "Channel A", "group": "TV Aberta"},
                "epg-b": {"drm": {"type": "declared"}},
            },
        )

        session = LinkedFakeSession()
        channels = load_json_catalog_channels([config], session=session)

        self.assertEqual(len(channels), 2)
        self.assertEqual(session.calls[1]["params"]["id"], "entry-a,entry-b")
        self.assertEqual(channels[0].name, "Channel A")
        self.assertEqual(channels[0].group, "TV Aberta")
        self.assertEqual(channels[0].tvg_id, "epg-a")
        self.assertEqual(channels[0].delivery_mode, "ssai")
        self.assertTrue(channels[0].requires_dynamic_resolution)
        self.assertFalse(channels[0].publishable_static)
        self.assertIsNone(channels[0].to_dict()["stream_url"])
        self.assertEqual(channels[1].drm, {"type": "declared"})


if __name__ == "__main__":
    unittest.main()
