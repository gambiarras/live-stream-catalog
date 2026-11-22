from tests import fixture_config  # noqa: F401

# isort: split

import json
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from live_stream_catalog.services.discovery import (
    DiscoveryCache,
    DiscoveryContext,
    discovery_scope,
)
from live_stream_catalog.services.video_platform_policy import (
    VideoPlatformRequestPolicy,
)
from live_stream_catalog.sources.video_platform_live_discovery import (
    VideoPlatformLiveDiscoveryConfig,
    load_video_platform_live_discovery_channels,
)


class FakeClient:
    calls = 0
    entries: ClassVar[list[dict[str, Any]]] = [
        {"id": "abcdefghijk", "title": "Live A", "live_status": "is_live"}
    ]
    error = None

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def extract_info(self, *_args, **_kwargs):
        type(self).calls += 1
        if type(self).error:
            raise type(self).error
        return {"entries": type(self).entries}


class DiscoveryScheduleTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 12, tzinfo=UTC)
        FakeClient.calls = 0
        FakeClient.error = None
        FakeClient.entries = [
            {"id": "abcdefghijk", "title": "Live A", "live_status": "is_live"}
        ]
        self.config = VideoPlatformLiveDiscoveryConfig(
            id="sample",
            name="Sample",
            logo="",
            group="news",
            handle="sample",
            cache_ttl_seconds=60,
            cache_lkg_seconds=180,
        )
        for target in (
            "live_stream_catalog.services.discovery.utc_now",
            "live_stream_catalog.sources.video_platform_live_discovery.utc_now",
            "live_stream_catalog.services.video_platform_policy.utc_now",
        ):
            clock = patch(target, side_effect=lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)

    def discover(self, directory, *, config=None, policy=None):
        context = DiscoveryContext(DiscoveryCache(Path(directory)))
        with discovery_scope(context):
            values = load_video_platform_live_discovery_channels(
                configs=[config or self.config],
                video_platform_dl_cls=FakeClient,
                video_platform_policy=policy,
                continue_on_error=False,
            )
        return values, context.observations

    def test_short_ttl_reuses_discovery_and_expiry_discovers_a_new_live_without_false_timestamps(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            first, original = self.discover(directory)
            observed_at = original["video_platform:sample"]["observed_at"]
            self.now += timedelta(seconds=30)
            FakeClient.entries = [
                {"id": "lmnopqrstuv", "title": "Live B", "live_status": "is_live"}
            ]
            cached, second = self.discover(directory)
            self.assertEqual(FakeClient.calls, 1)
            self.assertEqual(cached[0].id, first[0].id)
            self.assertEqual(second["video_platform:sample"]["origin"], "cache")
            self.assertEqual(
                second["video_platform:sample"]["observed_at"], observed_at
            )
            self.assertIsNone(cached[0].resolved_at)
            self.now += timedelta(seconds=31)
            fresh, third = self.discover(directory)
            self.assertEqual(FakeClient.calls, 2)
            self.assertNotEqual(fresh[0].id, first[0].id)
            self.assertEqual(third["video_platform:sample"]["origin"], "fresh")
            self.assertNotEqual(
                third["video_platform:sample"]["observed_at"], observed_at
            )

    def test_configuration_changes_invalidate_only_the_same_instances_single_snapshot(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            self.discover(directory)
            changed, _ = self.discover(
                directory,
                config=replace(
                    self.config, name="Updated display", handle="new-handle"
                ),
            )
            self.assertEqual(FakeClient.calls, 2)
            self.assertTrue(changed[0].name.startswith("Updated display"))
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 1)

    def test_valid_empty_inventory_is_cached_and_not_confused_with_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            FakeClient.entries = []
            empty_config = replace(self.config, use_fallback_when_empty=False)
            values, first = self.discover(directory, config=empty_config)
            self.assertEqual(values, [])
            self.assertTrue(first["video_platform:sample"]["complete"])
            self.assertEqual(first["video_platform:sample"]["reason"], "empty")
            FakeClient.error = RuntimeError("This must not be requested inside the TTL")
            cached, second = self.discover(directory, config=empty_config)
            self.assertEqual(cached, [])
            self.assertEqual(second["video_platform:sample"]["origin"], "cache")
            self.assertEqual(FakeClient.calls, 1)

    def test_malformed_inventory_is_an_error_and_preserves_last_known_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            original, observations = self.discover(directory)
            path = next(Path(directory).glob("*.json"))
            before = path.read_bytes()
            self.now += timedelta(seconds=61)
            for malformed in (None, {}, {"entries": None}, {"entries": "invalid"}):
                with patch.object(FakeClient, "extract_info", return_value=malformed):
                    values, retained = self.discover(directory)
                self.assertEqual(values[0].id, original[0].id)
                self.assertEqual(
                    retained["video_platform:sample"]["origin"], "retained"
                )
                self.assertFalse(retained["video_platform:sample"]["complete"])
                self.assertEqual(
                    retained["video_platform:sample"]["observed_at"],
                    observations["video_platform:sample"]["observed_at"],
                )
                self.assertEqual(path.read_bytes(), before)

    def test_errors_retain_a_bounded_lkg_without_renewing_its_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            first, original = self.discover(directory)
            path = next(Path(directory).glob("*.json"))
            payload = path.read_bytes()
            self.now += timedelta(seconds=61)
            FakeClient.error = RuntimeError("503 unavailable")
            retained, second = self.discover(directory)
            self.assertEqual(retained[0].id, first[0].id)
            self.assertEqual(second["video_platform:sample"]["origin"], "retained")
            self.assertFalse(second["video_platform:sample"]["complete"])
            self.assertEqual(
                second["video_platform:sample"]["observed_at"],
                original["video_platform:sample"]["observed_at"],
            )
            self.assertEqual(path.read_bytes(), payload)
            self.now += timedelta(seconds=121)
            with (
                self.assertLogs(level="ERROR"),
                self.assertRaisesRegex(RuntimeError, "503"),
            ):
                self.discover(directory)
            self.assertEqual(path.read_bytes(), payload)

    def test_cooldown_precedes_enumeration_and_reports_deferred_instead_of_fresh_cache(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            first, original = self.discover(directory)
            policy = VideoPlatformRequestPolicy(
                blocked_until=self.now + timedelta(hours=1)
            )
            values, second = self.discover(directory, policy=policy)
            self.assertEqual(values[0].id, first[0].id)
            self.assertEqual(FakeClient.calls, 1)
            self.assertEqual(second["video_platform:sample"]["origin"], "deferred")
            self.assertEqual(
                second["video_platform:sample"]["observed_at"],
                original["video_platform:sample"]["observed_at"],
            )
            self.assertFalse(second["video_platform:sample"]["complete"])
            self.now += timedelta(seconds=181)
            values, expired = self.discover(directory, policy=policy)
            self.assertEqual(values[0].id, self.config.id)
            self.assertEqual(expired["video_platform:sample"]["origin"], "deferred")
            self.assertIsNone(values[0].stream_url)
            self.assertEqual(FakeClient.calls, 1)

    def test_account_removal_is_authoritative_and_does_not_fall_back_to_live_cache(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            self.discover(directory)
            self.now += timedelta(seconds=61)
            FakeClient.error = RuntimeError("This channel does not exist")
            values, removed = self.discover(directory)
            self.assertTrue(values[0].removed)
            self.assertEqual(removed["video_platform:sample"]["reason"], "removed")
            self.assertTrue(removed["video_platform:sample"]["complete"])
            FakeClient.error = RuntimeError("No more requests are expected")
            cached, _ = self.discover(directory)
            self.assertTrue(cached[0].removed)
            self.assertEqual(FakeClient.calls, 2)

    def test_corrupt_cache_and_wrong_version_recover_through_fresh_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            self.discover(directory)
            path = next(Path(directory).glob("*.json"))
            for bad in (
                '{"incomplete"',
                '{"version":0}',
                '{"version":1,"channels":"invalid"}',
            ):
                path.write_text(bad)
                values, observations = self.discover(directory)
                self.assertEqual(len(values), 1)
                self.assertEqual(
                    observations["video_platform:sample"]["origin"], "fresh"
                )
                self.assertEqual(json.loads(path.read_text())["version"], 1)
            self.assertEqual(FakeClient.calls, 4)

    def test_cache_bounds_prune_only_known_private_snapshots_and_keep_empty_snapshots(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operator = root / "operator.json"
            operator.write_text('{"operator":"keep"}')
            cache = DiscoveryCache(root)
            with (
                patch("live_stream_catalog.services.discovery.MAX_CACHE_FILES", 2),
                patch("live_stream_catalog.services.discovery.MAX_CACHE_BYTES", 1000),
            ):
                for index in range(5):
                    cache.write(str(index), "fingerprint", [], self.now.isoformat())
            self.assertLessEqual(len(list(root.glob("*.json"))), 3)
            self.assertLessEqual(
                sum(
                    path.stat().st_size
                    for path in root.glob("*.json")
                    if path != operator
                ),
                1000,
            )
            self.assertTrue(operator.is_file())
            for path in root.glob("*.json"):
                if path != operator:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            cached = cache.read("4", "fingerprint", max_age_seconds=60)
            self.assertIsNotNone(cached)
            self.assertEqual(cached.channels, [])


if __name__ == "__main__":
    unittest.main()
