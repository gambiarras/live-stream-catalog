from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class AppConfig:
    output_path: Path
    meta_output_path: Path
    log_level: str
    max_workers: int
    default_resolution: str
    min_ttl_seconds: int

    @classmethod
    def from_args(cls, args) -> "AppConfig":
        return cls(
            output_path=Path(args.output),
            meta_output_path=Path(args.meta_output),
            log_level=args.log_level.upper(),
            max_workers=args.max_workers,
            default_resolution=args.default_resolution,
            min_ttl_seconds=getattr(args, "min_ttl", 1800),
        )