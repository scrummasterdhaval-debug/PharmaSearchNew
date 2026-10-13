from typing import Any

from core.logging_config import get_logger


logger = get_logger(__name__)


def run_swissmedic_search(substance: str) -> list[dict[str, Any]]:
    logger.info("Swissmedic connector is registered but not implemented yet")
    return []
