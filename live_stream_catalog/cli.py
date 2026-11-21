import argparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="live-stream-catalog",
        description="Builds and refreshes a static catalog of live stream URLs",
    )

    parser.add_argument("--output", default="channels.json")
    parser.add_argument("--meta-output", default="channels.meta.json")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--default-resolution", default="best")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("build")

    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("--min-ttl", type=int, default=1800)

    return parser.parse_args()