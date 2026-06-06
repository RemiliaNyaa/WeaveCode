from __future__ import annotations

import argparse
import sys

from weavecode.cli.commands.ping import cmd_ping
from weavecode.cli.commands.version import cmd_version
from weavecode.core.config import get_config
from weavecode.core.logging_setup import setup_logging


# CLI 主入口：解析命令行参数并分发到对应子命令
def main() -> None:
    parser = argparse.ArgumentParser(prog="weave", description="WeaveCode CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("ping", help="Ping the core daemon")

    args = parser.parse_args()

    if args.version:
        cmd_version()
        return

    config = get_config()
    setup_logging(config)

    if args.command == "ping":
        cmd_ping(config)
    else:
        parser.print_help()
        sys.exit(1)
