from tests import fixture_config  # noqa: F401

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_stream_catalog.io import load_channels_file, write_json_atomic


class JsonStoreTest(unittest.TestCase):
    def test_load_channels_file_returns_empty_for_missing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.json"

            self.assertEqual(load_channels_file(path), [])

    def test_write_json_atomic_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "payload.json"

            write_json_atomic(path, {"ok": True})

            self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "ok": true\n}')

    def test_failed_atomic_write_preserves_previous_file_and_removes_temporary_payload(self):
        for operation in ('json.dump', 'os.fsync', 'Path.replace'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / 'payload.json'
                path.write_text('{"previous":true}')
                with patch('live_stream_catalog.io.json_store.' + operation, side_effect=OSError('write failed')):
                    with self.assertRaises(OSError):
                        write_json_atomic(path, {'new': True})
                self.assertEqual(path.read_text(), '{"previous":true}')
                self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
