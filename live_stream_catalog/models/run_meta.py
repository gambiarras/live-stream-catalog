from dataclasses import asdict, dataclass


@dataclass(slots=True)
class RunMetadata:
    generated_at: str
    total_channels: int
    resolved_channels: int
    offline_channels: int
    removed_channels: int
    error_channels: int
    min_ttl_seconds: int | None
    max_ttl_seconds: int | None

    def to_dict(self) -> dict:
        return asdict(self)
