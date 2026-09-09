# Sidecar entry point — how the backend starts when it is a child of the
# Tauri desktop shell rather than something you launched in a terminal.
#
# Development is unchanged and still the documented path:
#
#     python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
#
# This file exists for the packaged build, where three things differ:
#
#   1. Nobody can see stderr. A traceback in a window that never opens is
#      indistinguishable from a hang, so startup failures are reported as
#      one plain line the shell can show the operator (DEPLOYMENT.md §8).
#   2. The shell needs a graceful stop it can trigger over HTTP, because
#      the shutdown handler is what cuts the throttle (§9). uvicorn's
#      Server object is created here so /shutdown has something to flip.
#   3. Loopback only. There is no LAN story yet, and binding 0.0.0.0
#      would put an authenticated-but-reachable throttle API on whatever
#      network the bench PC happens to be on.

from __future__ import annotations

import argparse
import os
import socket
import sys

import uvicorn

import main
from constants import APP_VERSION, DEFAULT_HOST, DEFAULT_PORT, LOG_SERVER

# Distinct from 1 so the shell can tell "someone else has the port" from
# "the backend crashed", and say so — those need different responses from
# the operator (close the other copy vs. send me the log).
EXIT_PORT_IN_USE = 3


def _port_is_free(host: str, port: int) -> bool:
    """Check before uvicorn does, so the failure is a sentence not a stack trace.

    Without SO_EXCLUSIVEADDRUSE semantics this is advisory — something
    could still take the port in the gap — but it catches the case that
    actually happens: a previous copy of the app still running.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="thrustbench-backend",
        description="NWA Bench Console backend (desktop sidecar)",
    )
    parser.add_argument("--host", default=os.environ.get("NWA_BENCH_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("NWA_BENCH_PORT", DEFAULT_PORT)))
    parser.add_argument("--version", action="version", version=APP_VERSION)
    return parser.parse_args(argv)


def build_server(host: str, port: int) -> uvicorn.Server:
    """Wire the running server to /shutdown.

    main.request_shutdown stays None under plain `uvicorn main:app`, and
    /shutdown answers 404 while it is None — so the endpoint is inert in
    development and in tests, and only becomes live for the process the
    shell actually launched.
    """
    config = uvicorn.Config(
        main.app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,           # ~20 Hz of telemetry would drown the useful lines
    )
    server = uvicorn.Server(config)

    def request_shutdown() -> None:
        # Graceful: uvicorn finishes the in-flight request (so the shell
        # gets its 200 back), then runs the FastAPI shutdown handler,
        # which aborts any profile and writes THR_MIN before the port
        # closes. A force-kill skips exactly that.
        server.should_exit = True

    main.request_shutdown = request_shutdown
    return server


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not _port_is_free(args.host, args.port):
        # Printed, not raised: the shell captures stdout/stderr and shows
        # it behind "SHOW DETAILS" when startup times out.
        print(f"{LOG_SERVER} Port {args.host}:{args.port} is already in use — "
              f"another copy of NWA Bench Console is probably still running.",
              file=sys.stderr, flush=True)
        return EXIT_PORT_IN_USE

    print(f"{LOG_SERVER} NWA Bench Console {APP_VERSION} starting on "
          f"http://{args.host}:{args.port}", flush=True)

    build_server(args.host, args.port).run()
    return 0


if __name__ == "__main__":
    sys.exit(run())
