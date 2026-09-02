import unittest
from unittest.mock import patch

from live_stream_catalog.sources.script_discovered_catalog import (
    DiscoveredRestCatalog,
    ScriptDiscoveredCatalogConfig,
    discover_script_urls,
    extract_rest_catalog_config,
    load_config_resource,
    load_rest_catalog_channels,
    row_to_channel,
)


class FakeResponse:
    def __init__(self, text="", payload=None):
        self.text = text
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.closed = False

    def get(self, url, headers=None, timeout=None):
        if url == "https://example.test/":
            return FakeResponse('<script src="/assets/app.js"></script>')

        if url == "https://example.test/assets/app.js":
            return FakeResponse(
                'const url = "https://project-ref.supabase.co";'
                'const key = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.signature_part";'
            )

        if url.startswith("https://project-ref.supabase.co/rest/v1/channels?"):
            return FakeResponse(
                payload=[
                    {
                        "id": "row-id",
                        "name": "Sample Channel",
                        "epg_slug": "sample-channel",
                        "stream_url": "https://media.example.test/live.m3u8",
                        "logo_url": "https://media.example.test/logo.png",
                        "tvg_id": "SampleChannel.br",
                        "stream_status": "active",
                        "categories": {"name": "news"},
                    }
                ]
            )

        raise AssertionError(f"Unexpected URL: {url}")

    def close(self):
        self.closed = True


class ScriptDiscoveredCatalogTest(unittest.TestCase):
    def test_loads_site_url_from_environment(self):
        with patch.dict(
            "os.environ",
            {"LIVE_REST_CATALOG_URL": "https://example.test/"},
        ):
            configs = load_config_resource()

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].site_url, "https://example.test/")
        self.assertEqual(configs[0].provider_id, "rest_catalog_1")

    def test_discovers_absolute_script_urls(self):
        html = """
        <html>
          <head>
            <script src="/assets/app.js"></script>
            <script src="https://cdn.example.test/vendor.js"></script>
          </head>
        </html>
        """

        self.assertEqual(
            discover_script_urls("https://example.test/", html),
            [
                "https://example.test/assets/app.js",
                "https://cdn.example.test/vendor.js",
            ],
        )

    def test_extracts_rest_catalog_configuration_from_js(self):
        key = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.signature_part"
        js_content = f"""
        const url = "https://project-ref.supabase.co";
        const anon = "{key}";
        """

        api_base_urls, anon_keys = extract_rest_catalog_config(js_content)

        self.assertEqual(api_base_urls, ["https://project-ref.supabase.co"])
        self.assertEqual(anon_keys, [key])

    def test_builds_rest_endpoint_with_encoded_query(self):
        catalog = DiscoveredRestCatalog(
            site_url="https://example.test/",
            script_url="https://example.test/assets/app.js",
            api_base_url="https://project-ref.supabase.co",
            anon_key="token",
        )

        endpoint = catalog.endpoint(
            table_name="channels",
            select="*,categories(name)",
            filters={"is_active": "eq.true"},
            order="channel_number.asc",
        )

        self.assertEqual(
            endpoint,
            "https://project-ref.supabase.co/rest/v1/channels"
            "?select=%2A%2Ccategories%28name%29"
            "&is_active=eq.true"
            "&order=channel_number.asc",
        )

    def test_maps_catalog_row_to_channel(self):
        channel = row_to_channel(
            {
                "id": "sample",
                "name": "Sample Channel",
                "stream_url": "https://media.example.test/live.m3u8",
                "logo_url": "https://media.example.test/logo.png",
                "xmltv_id": "SampleChannel.br",
                "categories": {"name": "news"},
            },
            source_type="script_discovered",
        )

        self.assertEqual(channel.id, "sample")
        self.assertEqual(channel.name, "Sample Channel")
        self.assertEqual(channel.source_url, "https://media.example.test/live.m3u8")
        self.assertEqual(channel.stream_url, "https://media.example.test/live.m3u8")
        self.assertEqual(channel.logo, "https://media.example.test/logo.png")
        self.assertEqual(channel.group, "news")
        self.assertEqual(channel.source_type, "script_discovered")
        self.assertEqual(channel.tvg_id, "SampleChannel.br")
        self.assertEqual(channel.status, "resolved")
        self.assertIsNone(channel.ttl_seconds)

    def test_loads_channels_from_script_discovered_rest_catalog(self):
        config = ScriptDiscoveredCatalogConfig(
            id="catalog_1",
            site_url="https://example.test/",
        )
        session = FakeSession()

        channels = load_rest_catalog_channels([config], session=session)

        self.assertEqual(len(channels), 1)
        self.assertFalse(session.closed)
        self.assertEqual(channels[0].id, "catalog_1.sample-channel")
        self.assertEqual(channels[0].name, "Sample Channel")
        self.assertEqual(channels[0].stream_url, "https://media.example.test/live.m3u8")
        self.assertEqual(channels[0].logo, "https://media.example.test/logo.png")
        self.assertEqual(channels[0].group, "news")
        self.assertEqual(channels[0].source_type, "script_discovered")
        self.assertEqual(channels[0].tvg_id, "SampleChannel.br")
        self.assertEqual(channels[0].status, "resolved")


if __name__ == "__main__":
    unittest.main()
