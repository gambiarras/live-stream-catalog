def get_resource_registry() -> list[tuple[str, str]]:
    return [
        ("game_platform_channels.json", "game_platform"),
        ("live_platform_channels.json", "live_platform"),
    ]
