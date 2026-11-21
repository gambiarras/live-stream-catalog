from streamlink import Streamlink

from live_stream_catalog.plugins.live_platform import register_live_platform_plugin

def register_custom_plugins(session: Streamlink) -> None:
    register_live_platform_plugin(session)