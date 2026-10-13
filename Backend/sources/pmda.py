from typing import Any

from core.logging_config import get_logger


logger = get_logger(__name__)


def run_pmda_search(substance: str) -> list[dict[str, Any]]:
    logger.info("PMDA Japan connector is registered but not implemented yet")
    return []
