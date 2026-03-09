"""CLI entry point for pre-flight health checks.

Usage:
    python -m bot.preflight_check              # uses config.yaml
    python -m bot.preflight_check path/to.yaml
"""

import asyncio
import sys

from bot.config import load_config
from bot.logger import setup_logging
from bot.preflight import PreflightError, run_preflight


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    config = load_config(config_path)
    setup_logging(config.log_file, config.log_level)

    try:
        asyncio.run(run_preflight(config))
        print("Pre-flight: ALL CHECKS PASSED")
        sys.exit(0)
    except PreflightError as e:
        print("Pre-flight FAILED: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
