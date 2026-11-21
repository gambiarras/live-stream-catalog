from live_stream_catalog.cli import parse_args
from live_stream_catalog.config import AppConfig
from live_stream_catalog.local_config import require_local_configuration
from live_stream_catalog.logging_config import configure_logging


def main() -> None:
    args = parse_args()
    config = AppConfig.from_args(args)

    configure_logging(config.log_level)
    require_local_configuration()

    if args.command == "build":
        from live_stream_catalog.services.build import run_build

        run_build(config)
        return

    if args.command == "refresh":
        from live_stream_catalog.services.refresh import run_refresh

        run_refresh(config)
        return

    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
