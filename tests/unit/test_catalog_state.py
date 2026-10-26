import unittest
from dataclasses import replace

from live_stream_catalog.models import Channel
from live_stream_catalog.services.catalog_state import (
    carry_previous_resolution,
    split_refresh_candidates,
)


def make_channel(**changes) -> Channel:
    channel = Channel(
        id="fixture.channel",
        name="Fixture Channel",
        source_url="https://example.test/channel",
        logo="",
        group="general",
        source_type="youtube",
    )
    return replace(channel, **changes)


class CatalogStateTest(unittest.TestCase):
    def test_keeps_new_discovery_url_and_resolution_metadata(self):
        previous = make_channel(
            stream_url="https://old.example.test/live.m3u8",
            status="resolved",
            resolved_at="2000-01-01T00:00:00+00:00",
            expires_at="2000-01-02T00:00:00+00:00",
            ttl_seconds=0,
        )
        discovered = make_channel(
            stream_url="https://new.example.test/live.m3u8",
            status="resolved",
            resolved_at="2026-10-26T00:00:00+00:00",
            expires_at="2100-01-01T00:00:00+00:00",
            ttl_seconds=3600,
        )
        expected = discovered.to_dict()

        result = carry_previous_resolution([discovered], [previous])

        self.assertEqual(result[0].to_dict(), expected)
        self.assertEqual(split_refresh_candidates(result, 1800), (result, []))

    def test_same_url_does_not_overwrite_new_discovery_metadata(self):
        previous = make_channel(
            stream_url="https://example.test/live.m3u8",
            status="resolved",
            expires_at="2000-01-01T00:00:00+00:00",
        )
        discovered = replace(previous, expires_at="2100-01-01T00:00:00+00:00")

        result = carry_previous_resolution([discovered], [previous])

        self.assertEqual(result[0].expires_at, "2100-01-01T00:00:00+00:00")

    def test_reuses_resolution_without_overwriting_new_presentation(self):
        previous = make_channel(
            stream_url="https://example.test/live.m3u8",
            status="resolved",
            resolved_at="2026-10-26T00:00:00+00:00",
            expires_at="2100-01-01T00:00:00+00:00",
            ttl_seconds=3600,
        )
        discovered = make_channel(name="Updated title", group="news")

        result = carry_previous_resolution([discovered], [previous])

        self.assertEqual(result[0].stream_url, previous.stream_url)
        self.assertEqual(result[0].resolved_at, previous.resolved_at)
        self.assertEqual(result[0].name, "Updated title")
        self.assertEqual(result[0].group, "news")

    def test_changed_resolution_inputs_do_not_reuse_old_transport(self):
        previous = make_channel(
            stream_url="https://example.test/live.m3u8", status="resolved"
        )
        changes = [
            {"source_url": "https://example.test/another-channel"},
            {"source_type": "twitch"},
            {"provider_id": "another-provider"},
            {"logical_channel_id": "another-feed"},
            {"variant_id": "another-variant"},
            {"resolution": "720p"},
            {"codec": "hevc"},
            {"protocol": "dash"},
            {"request_headers": {"Referer": "https://example.test/updated"}},
            {"secret_refs": {"authorization": "UPDATED_AUTH"}},
            {"requires_dynamic_resolution": True},
            {"publishable_static": False},
            {"delivery_mode": "ssai"},
            {"drm": {"type": "widevine"}},
        ]

        for values in changes:
            with self.subTest(fields=tuple(values)):
                discovered = make_channel(**values)
                result = carry_previous_resolution([discovered], [previous])
                self.assertIsNone(result[0].stream_url)
                self.assertEqual(result[0].status, "pending")
                self.assertEqual(split_refresh_candidates(result, 1800), ([], result))

    def test_fresh_discovery_failure_does_not_become_cached_success(self):
        previous = make_channel(
            stream_url="https://example.test/live.m3u8", status="resolved"
        )
        for status in ("offline", "error"):
            with self.subTest(status=status):
                discovered = make_channel(status=status, error="discovery_failure")
                result = carry_previous_resolution([discovered], [previous])
                self.assertEqual(result[0].status, status)
                self.assertIsNone(result[0].stream_url)
                self.assertEqual(result[0].error, "discovery_failure")

    def test_does_not_reuse_an_unsuccessful_previous_resolution(self):
        for status in ("pending", "offline", "error"):
            with self.subTest(status=status):
                previous = make_channel(
                    stream_url="https://example.test/live.m3u8", status=status
                )
                result = carry_previous_resolution([make_channel()], [previous])
                self.assertIsNone(result[0].stream_url)

    def test_expired_previous_candidate_still_requires_resolution(self):
        previous = make_channel(
            stream_url="https://example.test/live.m3u8",
            status="resolved",
            expires_at="2000-01-01T00:00:00+00:00",
            ttl_seconds=0,
        )

        result = carry_previous_resolution([make_channel()], [previous])

        self.assertEqual(split_refresh_candidates(result, 1800), ([], result))

    def test_previous_removal_is_not_resurrected_by_new_url(self):
        previous = make_channel(
            status="removed", removed=True, removal_reason="channel_not_found"
        )
        discovered = make_channel(
            stream_url="https://new.example.test/live.m3u8", status="resolved"
        )

        result = carry_previous_resolution([discovered], [previous])

        self.assertEqual(result[0].status, "removed")
        self.assertTrue(result[0].removed)
        self.assertIsNone(result[0].stream_url)
        self.assertFalse(result[0].publishable_static)

    def test_new_removal_is_not_overwritten_by_previous_success(self):
        previous = make_channel(
            stream_url="https://example.test/live.m3u8", status="resolved"
        )
        discovered = make_channel(
            status="removed", removed=True, publishable_static=False
        )

        result = carry_previous_resolution([discovered], [previous])

        self.assertEqual(result[0].status, "removed")
        self.assertIsNone(result[0].stream_url)

    def test_same_id_from_another_provider_does_not_shadow_matching_source(self):
        matching = make_channel(
            stream_url="https://example.test/live.m3u8", status="resolved"
        )
        unrelated = replace(
            matching, provider_id="another-provider", stream_url="https://other.test/live"
        )

        result = carry_previous_resolution([make_channel()], [matching, unrelated])

        self.assertEqual(result[0].stream_url, matching.stream_url)


if __name__ == "__main__":
    unittest.main()
