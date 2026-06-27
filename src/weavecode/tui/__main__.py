from __future__ import annotations

import argparse

from weavecode.tui.app import WeaveTuiApp

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437


# weave-tui 启动入口：解析命令行参数后构造 WeaveTuiApp 并进入 Textual 事件循环
def main() -> None:
    parser = argparse.ArgumentParser(prog="weave-tui", description="WeaveCode TUI")
    parser.add_argument("--host", default=_DEFAULT_HOST, help="daemon host")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT, help="daemon port")
    parser.add_argument(
        "--replay",
        metavar="RUN_ID",
        help="Replay events from a past run on connect",
    )
    args = parser.parse_args()

    app = WeaveTuiApp(args.host, args.port, replay_run_id=args.replay)
    app.run()


if __name__ == "__main__":
    main()
