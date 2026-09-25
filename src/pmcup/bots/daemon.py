"""Background entrypoint: python -m pmcup.bots.daemon"""

from __future__ import annotations

from ..config import settings
from .runner import run_forever, setup_logging


def main() -> None:
    setup_logging()
    run_forever(settings)


if __name__ == "__main__":
    main()
