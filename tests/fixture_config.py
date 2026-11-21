"""Isolate tests from private deployment inputs using fictional local configuration."""

import os
from pathlib import Path

CONFIG_DIR = str(Path(__file__).parent / "fixtures" / "local")
os.environ["IPTV_LOCAL_CONFIG_DIR"] = CONFIG_DIR
