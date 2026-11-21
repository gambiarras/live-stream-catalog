from tests import fixture_config  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_stream_catalog.config import AppConfig
from live_stream_catalog.local_config import (
    local_config_dir, require_local_configuration, resource_json, runtime_settings,
    source_alias, discovery_client_factory,
)
from live_stream_catalog.services.build import run_build
from live_stream_catalog.services.refresh import run_refresh


class LocalConfigTest(unittest.TestCase):
    def config(self, folder):
        return AppConfig(output_path=folder / "channels.json", meta_output_path=folder / "meta.json",
                         log_level="INFO", max_workers=1, default_resolution="best", min_ttl_seconds=1800)

    def test_missing_private_inputs_preserve_both_published_artifacts_before_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = self.config(folder)
            config.output_path.write_text('[{"id":"last-publication"}]')
            config.meta_output_path.write_text('{"last":"valid"}')
            with patch.dict("os.environ", {"IPTV_LOCAL_CONFIG_DIR": str(folder / "missing")}):
                for run in (run_build, run_refresh):
                    with self.subTest(run=run.__name__), \
                         patch("live_stream_catalog.services.build.load_catalog") as discovery:
                        with self.assertRaisesRegex(RuntimeError, "Local catalog configuration is required"):
                            run(config)
                        discovery.assert_not_called()
                self.assertEqual(resource_json("game_platform_channels.json"), [])
                self.assertEqual(local_config_dir(), folder / "missing")
            self.assertEqual(config.output_path.read_text(), '[{"id":"last-publication"}]')
            self.assertEqual(config.meta_output_path.read_text(), '{"last":"valid"}')

    def test_no_reviewed_resources_and_invalid_runtime_are_rejected_without_values(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            runtime = folder / "runtime.json"
            with patch.dict("os.environ", {"IPTV_LOCAL_CONFIG_DIR": directory}):
                runtime.write_text('{"enabled":true}')
                with self.assertRaisesRegex(RuntimeError, "source resources are missing"):
                    require_local_configuration()
                runtime.write_text('"private-endpoint"')
                with self.assertRaisesRegex(ValueError, "Invalid local catalog runtime configuration") as error:
                    runtime_settings()
                self.assertNotIn("private-endpoint", str(error.exception))

    def test_empty_build_preserves_the_previous_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            config.output_path.write_text('[]')
            config.meta_output_path.write_text('{"previous":true}')
            with patch("live_stream_catalog.services.build.load_catalog", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "No configured catalog candidates"):
                    run_build(config)
            self.assertEqual(config.output_path.read_text(), '[]')
            self.assertEqual(config.meta_output_path.read_text(), '{"previous":true}')

    def test_private_resources_and_identity_aliases_are_operator_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "resources").mkdir()
            (folder / "resources" / "game_platform_channels.json").write_text('[{"id":"private-channel"}]')
            (folder / "runtime.json").write_text(json.dumps({
                "enabled": True, "source_type_aliases": {"legacy-source": "game_platform"},
                "provider_aliases": {"legacy-provider": "game_platform"},
            }))
            with patch.dict("os.environ", {"IPTV_LOCAL_CONFIG_DIR": directory}):
                require_local_configuration()
                self.assertEqual(resource_json("game_platform_channels.json"), [{"id": "private-channel"}])
                self.assertEqual(source_alias("legacy-source"), "game_platform")
                self.assertEqual(source_alias("legacy-provider", provider=True), "game_platform")

    def test_discovery_factory_is_local_and_has_no_provider_default(self):
        with patch("live_stream_catalog.local_config.source_settings", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "Local discovery client factory is required"):
                discovery_client_factory()
        with patch("live_stream_catalog.local_config.source_settings", return_value={"discovery_factory": "json.JSONDecoder"}):
            self.assertIs(discovery_client_factory(), json.JSONDecoder)


if __name__ == "__main__":
    unittest.main()
