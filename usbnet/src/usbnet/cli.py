from __future__ import annotations

import argparse
import sys

from . import client, config, server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one side of the USBnet tunnel or configure its board.",
        add_help=False,
        epilog=(
            "Examples:\n"
            "  usbnet --client [--port 8080]\n"
            "  usbnet --server\n"
            "  usbnet --config list"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    roles = parser.add_mutually_exclusive_group()
    roles.add_argument("--role", choices=("client", "server", "config"),
                       help="select a role")
    roles.add_argument("--client", dest="role", action="store_const", const="client",
                       help="run the local proxy")
    roles.add_argument("--server", dest="role", action="store_const", const="server",
                       help="run the upstream TCP server")
    roles.add_argument("--config", dest="role", action="store_const", const="config",
                       help="configure the USBnet board")
    parser.add_argument("--port", type=int,
                        help="local HTTP proxy port (client role only; default 8080)")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args, remainder = parser.parse_known_args(argv)

    if args.role is None:
        if "-h" in remainder or "--help" in remainder:
            parser.print_help()
            return 0
        parser.error("select --client, --server, or --config")

    if args.role in ("client", "server") and ("-h" in remainder or "--help" in remainder):
        parser.print_help()
        return 0
    if args.role in ("client", "server") and remainder:
        parser.error(f"unexpected arguments for --{args.role}: {' '.join(remainder)}")
    if args.role != "client" and args.port is not None:
        parser.error("--port is only valid with the client role")

    try:
        if args.role == "client":
            port = client.DEFAULT_PORT if args.port is None else args.port
            if not 1 <= port <= 65535:
                parser.error("--port must be from 1 to 65535")
            client.ClientProxy(port=port).serve_forever()
            return 0
        if args.role == "server":
            server.USBnetServer().serve_forever()
            return 0
        if args.role == "config":
            return config.main(remainder)
        parser.error(f"unsupported role: {args.role}")
    except KeyboardInterrupt:
        return 0
    except (OSError, ImportError, ValueError, TimeoutError) as error:
        print(error, file=sys.stderr)
        return 1
