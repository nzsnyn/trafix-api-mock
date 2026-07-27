"""Entrypoint for the API server.

Run with ``python -m trafix.server``. Replaces ``php artisan serve`` / the
nginx+php-fpm pair the Laravel app runs behind.
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from trafix.api import build
from trafix.config import load_config

log = logging.getLogger("server")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trafix parking API server")
    parser.add_argument("--env", default=None, help="config environment override")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)-12s | %(message)s",
    )

    config = load_config(args.env)
    app = build(config)

    host = args.host or config.api.host
    port = args.port or config.api.port
    log.info("serving the API on %s:%s (env=%s)", host, port, config.env)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
