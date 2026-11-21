from tests import fixture_config  # noqa: F401

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from live_stream_catalog.models import Channel
from live_stream_catalog.services.resolver import resolve_channels
from live_stream_catalog.services.refresh import _load_dynamic_channels
from live_stream_catalog.config import AppConfig
from live_stream_catalog.sources import load_catalog
from live_stream_catalog.services.build import run_build
from live_stream_catalog.services.video_platform_policy import VideoPlatformRequestPolicy, is_video_platform_rate_limit
from live_stream_catalog.sources.video_platform_live_discovery import (
    VideoPlatformLiveDiscoveryConfig, load_video_platform_live_discovery_channels,
)


def channel(name="sample", **values):
    return Channel(id=name, name=name, source_url=f"https://video.example.test/watch?v={name}",
                   source_type="video_platform", logo="", group="general", **values)


class VideoPlatformPolicyTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
        self.clock = patch("live_stream_catalog.services.video_platform_policy.utc_now", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_escalates_persistently_and_suppresses_the_whole_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "channels.json"
            for hours in (1, 2, 4, 8, 8):
                policy = VideoPlatformRequestPolicy.for_output(output)
                self.assertTrue(policy.allowed())
                with self.assertLogs("live_stream_catalog.services.video_platform_policy", level="WARNING"):
                    policy.block(RuntimeError("HTTP 429 token=never-persist-this"))
                self.assertEqual(policy.blocked_until, self.now + timedelta(hours=hours))
                self.assertFalse(policy.allowed())
                policy.block(RuntimeError("429"))
                self.assertEqual(policy.blocked_until, self.now + timedelta(hours=hours))
                retained = VideoPlatformRequestPolicy.for_output(output)
                self.assertFalse(retained.allowed())
                state = policy.state_file.read_text()
                self.assertNotIn("token", state)
                self.assertNotIn("never-persist", state)
                self.now += timedelta(hours=hours, seconds=1)

    def test_honors_retry_after_seconds_date_and_caps_unbounded_values(self):
        for value, delay in (("7200", 7200), ("Sat, 21 Nov 2026 15:00:00 GMT", 10800),
                             ("999999999999999999", 86400), ("invalid", 3600)):
            with self.subTest(value=value):
                policy = VideoPlatformRequestPolicy()
                cause = RuntimeError("429")
                cause.response = Mock(headers={"Retry-After": value})
                outer = RuntimeError("HTTP 429")
                outer.__cause__ = cause
                with self.assertLogs("live_stream_catalog.services.video_platform_policy", level="WARNING"):
                    policy.block(outer)
                self.assertEqual(policy.blocked_until, self.now + timedelta(seconds=delay))

    def test_stale_or_nonexistent_success_does_not_clear_cooldown(self):
        policy = VideoPlatformRequestPolicy(strikes=2, blocked_until=self.now + timedelta(hours=1))
        policy.finish()
        self.assertFalse(policy.allowed())
        self.assertEqual(policy.strikes, 2)
        policy.successful_resolution = True
        policy.rate_limited = True
        policy.finish()
        self.assertEqual(policy.strikes, 2)

    def test_complete_recovered_run_resets_escalation_and_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = VideoPlatformRequestPolicy.for_output(Path(directory) / "channels.json")
            with self.assertLogs("live_stream_catalog.services.video_platform_policy", level="WARNING"):
                policy.block(RuntimeError("429"))
            policy.rate_limited = False
            policy.successful_resolution = True
            policy.finish()
            self.assertTrue(policy.allowed())
            self.assertEqual(policy.strikes, 0)
            self.assertFalse(policy.state_file.exists())

    def test_rejects_invalid_oversized_and_naive_state_and_supports_runtime_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "state.json"
            path.parent.mkdir()
            with patch.dict("os.environ", {"LIVE_VIDEO_PLATFORM_STATE_FILE": str(path)}):
                for payload in ([], {}, {"strikes": 2, "blocked_until": "2026-11-21T12:00:00"}, "x" * 5000):
                    path.write_text(json.dumps(payload))
                    with self.assertLogs("live_stream_catalog.services.video_platform_policy", level="WARNING"):
                        policy = VideoPlatformRequestPolicy.for_output(Path(directory) / "channels.json")
                    self.assertTrue(policy.allowed())
                    self.assertEqual(policy.state_file, path)

    def test_only_first_rate_limited_resolver_attempt_hits_video_platform_and_other_sources_continue(self):
        policy = VideoPlatformRequestPolicy()
        session = Mock()
        session.streams.side_effect = RuntimeError("HTTP 429 Too Many Requests")
        direct = Channel(id="direct", name="Direct", source_url="https://public.example/live.m3u8",
                         stream_url="https://public.example/live.m3u8", source_type="directory_catalog",
                         logo="", group="general", status="resolved")
        with patch("live_stream_catalog.services.resolver.build_streamlink_session", return_value=session), \
             self.assertLogs("live_stream_catalog.services.video_platform_policy", level="WARNING"):
            result = resolve_channels([channel(str(i)) for i in range(20)] + [direct],
                                      max_workers=4, video_platform_policy=policy)
        session.streams.assert_called_once()
        self.assertEqual(len(result), 21)
        self.assertEqual(result[0].id, "direct")
        self.assertEqual(result[0].status, "resolved")
        self.assertTrue(all(item.error == "video_platform_rate_limited" for item in result[1:]))

    def test_discovery_rate_limit_blocks_remaining_discovery_and_all_resolution(self):
        policy = VideoPlatformRequestPolicy()
        configs = [VideoPlatformLiveDiscoveryConfig(id=f"c{i}", name=f"Channel {i}", handle=f"c{i}",
                                               logo="", group="general") for i in range(10)]
        with patch("live_stream_catalog.sources.video_platform_live_discovery.discover_live_channels",
                   side_effect=RuntimeError("HTTP 429")) as discover, \
             patch("live_stream_catalog.services.resolver.build_streamlink_session") as session, \
             self.assertLogs(level="WARNING"):
            found = load_video_platform_live_discovery_channels(configs=configs, video_platform_policy=policy)
            result = resolve_channels(found, max_workers=4, video_platform_policy=policy)
        discover.assert_called_once()
        session.assert_not_called()
        self.assertEqual(len(result), 10)
        self.assertTrue(all(not item.publishable_static for item in result))
        self.assertTrue(all(item.requires_dynamic_resolution for item in result))
        self.assertTrue(all(not item.removed for item in result))

    def test_cooldown_reuses_only_known_future_expiry_without_renewing_success_timestamp(self):
        policy = VideoPlatformRequestPolicy(blocked_until=self.now + timedelta(hours=2))
        observed = (self.now - timedelta(hours=1)).isoformat()
        expiry = (self.now + timedelta(hours=1)).isoformat()
        candidates = [channel("future", stream_url="https://media.example/live.m3u8", expires_at=expiry,
                              status="resolved", resolved_at=observed),
                      channel("expired", stream_url="https://media.example/expired.m3u8",
                              expires_at=(self.now - timedelta(minutes=1)).isoformat(),
                              status="resolved", resolved_at=observed),
                      channel("unknown", stream_url="https://media.example/unknown.m3u8",
                              status="resolved", resolved_at=observed)]
        with patch("live_stream_catalog.services.resolver.utc_now", return_value=self.now), \
             patch("live_stream_catalog.services.resolver.build_streamlink_session") as session:
            result = resolve_channels(candidates, max_workers=1, video_platform_policy=policy)
        session.assert_not_called()
        self.assertEqual(result[0].status, "resolved")
        self.assertEqual(result[0].ttl_seconds, 3600)
        self.assertEqual(result[0].resolved_at, observed)
        self.assertIsNone(result[0].to_dict()["stream_url"])
        self.assertTrue(all(item.stream_url is None for item in result[1:]))
        self.assertTrue(all(item.resolved_at == observed for item in result))
        self.assertTrue(all(not item.publishable_static for item in result))

    def test_rate_limit_classifier_does_not_treat_other_http_failures_as_blocks(self):
        self.assertFalse(is_video_platform_rate_limit(RuntimeError("503 service unavailable")))
        self.assertFalse(is_video_platform_rate_limit(RuntimeError("video abc429def unavailable")))
        self.assertTrue(is_video_platform_rate_limit(RuntimeError("429 Too Many Requests")))

    def test_refresh_reuses_previous_identities_on_discovery_block_without_retrying_resolution(self):
        config = AppConfig(output_path=Path("unused.json"), meta_output_path=Path("unused.meta.json"),
                           log_level="INFO", max_workers=1, default_resolution="best", min_ttl_seconds=1800)
        observed = (self.now - timedelta(hours=1)).isoformat()
        known = channel("known-video", resolved_at=observed, status="resolved")
        policy = VideoPlatformRequestPolicy()
        configs = [VideoPlatformLiveDiscoveryConfig(id=f"c{i}", name=f"Channel {i}", handle=f"c{i}",
                                               logo="", group="general") for i in range(3)]
        with patch("live_stream_catalog.services.refresh.load_configured_addon_catalogs", return_value=[]), \
             patch("live_stream_catalog.services.refresh.load_configured_json_catalogs", return_value=[]), \
             patch("live_stream_catalog.services.refresh.load_configured_rest_catalogs", return_value=[]), \
             patch("live_stream_catalog.sources.video_platform_live_discovery.load_config_resource", return_value=configs), \
             patch("live_stream_catalog.sources.video_platform_live_discovery.discover_live_channels",
                   side_effect=RuntimeError("429 Too Many Requests")) as discover, \
             patch("live_stream_catalog.services.resolver.build_streamlink_session") as session, \
             self.assertLogs(level="WARNING"):
            first = _load_dynamic_channels(config, [known], video_platform_policy=policy)
            self.assertEqual([item.id for item in first], ["known-video"])
            second = _load_dynamic_channels(config, first, video_platform_policy=policy)
            result = resolve_channels(second, max_workers=1, video_platform_policy=policy)
        discover.assert_called_once()
        session.assert_not_called()
        self.assertEqual(result[0].id, "known-video")
        self.assertEqual(result[0].resolved_at, observed)
        self.assertIsNone(result[0].stream_url)
        self.assertFalse(result[0].removed)

    def test_media_host_expire_query_cannot_survive_a_cooldown_when_expired(self):
        policy = VideoPlatformRequestPolicy(blocked_until=self.now + timedelta(hours=2))
        known = channel("expired", stream_url="https://rr.media.example.test/videoplayback?expire=946684800",
                        resolved_at=self.now.isoformat(), status="resolved")
        result = resolve_channels([known], max_workers=1, video_platform_policy=policy)
        self.assertIsNone(result[0].stream_url)
        self.assertEqual(result[0].status, "error")

    def test_restart_skips_discovery_and_resolver_network_for_persisted_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "channels.json"
            policy = VideoPlatformRequestPolicy.for_output(output)
            with self.assertLogs(level="WARNING"):
                policy.block(RuntimeError("429"))
            restarted = VideoPlatformRequestPolicy.for_output(output)
            config = VideoPlatformLiveDiscoveryConfig(id="c", name="Channel", handle="c", logo="", group="general")
            with patch("live_stream_catalog.sources.video_platform_live_discovery.discover_live_channels") as discovery, \
                 patch("live_stream_catalog.services.resolver.build_streamlink_session") as session:
                candidates = load_video_platform_live_discovery_channels(configs=[config], video_platform_policy=restarted)
                result = resolve_channels(candidates, max_workers=1, video_platform_policy=restarted)
            discovery.assert_not_called()
            session.assert_not_called()
            self.assertEqual(result[0].error, "video_platform_rate_limited")

    def test_catalog_facade_forwards_the_same_policy_to_the_source_loader(self):
        policy = VideoPlatformRequestPolicy()
        with patch("live_stream_catalog.sources.loader.load_catalog", return_value=[]) as loader:
            self.assertEqual(load_catalog(default_resolution="720p", video_platform_policy=policy), [])
        loader.assert_called_once_with(default_resolution="720p", video_platform_policy=policy)

    def test_build_uses_real_facade_loader_and_discovery_with_persistent_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "channels.json"
            policy = VideoPlatformRequestPolicy.for_output(output)
            with self.assertLogs(level="WARNING"):
                policy.block(RuntimeError("HTTP 429"))
            config = AppConfig(output_path=output, meta_output_path=Path(directory) / "channels.meta.json",
                               log_level="INFO", max_workers=1, default_resolution="best", min_ttl_seconds=1800)
            # Keep the source facade, generic loader and platform adapter real.
            # Other adapters are independent and no network client may be opened.
            with patch("live_stream_catalog.sources.loader.load_configured_addon_catalogs", return_value=[]), \
                 patch("live_stream_catalog.sources.loader.load_configured_json_catalogs", return_value=[]), \
                 patch("live_stream_catalog.sources.loader.load_configured_rest_catalogs", return_value=[]), \
                 patch("live_stream_catalog.sources.loader.load_configured_directory_catalog", return_value=[]), \
                 patch("live_stream_catalog.sources.video_platform_live_discovery.discover_live_channels") as discovery, \
                 patch("live_stream_catalog.services.resolver.build_streamlink_session") as session:
                run_build(config)
            discovery.assert_not_called()
            session.assert_not_called()
            payload = json.loads(output.read_text())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["source_type"], "video_platform")
            self.assertEqual(payload[0]["error"], "video_platform_rate_limited")
            self.assertIsNone(payload[0]["stream_url"])
            self.assertIsNone(payload[0]["resolved_at"])
            self.assertFalse(payload[0]["publishable_static"])
            self.assertTrue(policy.state_file.exists())
            self.assertNotIn("blocked_until", output.read_text())


if __name__ == "__main__":
    unittest.main()
