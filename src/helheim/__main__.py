"""Entry point: one process on loopback, nothing else."""

from __future__ import annotations

import uvicorn

from .config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "helheim.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=settings.mode != "hosted",
    )


if __name__ == "__main__":
    main()
