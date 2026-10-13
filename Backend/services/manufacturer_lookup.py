"""Automated manufacturer lookup from multiple sources.

Fills missing manufacturer data using:
1. Company websites
2. Trade databases (GS1, UNICHEM, DrugBank, etc.)
3. Public registries

Data is validated and flagged for admin review.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import requests
from core.logging_config import get_logger

logger = get_logger(__name__)

# Configuration
SEARCH_TIMEOUT = 10
MAX_RETRIES = 3
CONFIDENCE_THRESHOLD = 0.7  # 70% match = acceptable

# Trade database APIs
SOURCES = {
    "drugbank": {
        "name": "DrugBank",
        "url": "https://www.drugbank.ca/search",
        "type": "web_search",
    },
    "unichem": {
        "name": "UNICHEM",
        "url": "https://www.ebi.ac.uk/unichem/",
        "type": "api",
    },
    "company_website": {
        "name": "Company Website",
        "url": None,
        "type": "web_scrape",
    },
    "openfda": {
        "name": "OpenFDA",
        "url": "https://api.fda.gov/drug/ndc.json",
        "type": "api",
    },
}


def _similarity_score(str1: str, str2: str) -> float:
    """Calculate string similarity (0-1). Simple Levenshtein-based."""
    if not str1 or not str2:
        return 0.0

    str1 = str1.lower().strip()
    str2 = str2.lower().strip()

    if str1 == str2:
        return 1.0

    # Check if one contains the other
    if str1 in str2 or str2 in str1:
        return 0.8

    # Levenshtein distance
    if len(str1) == 0 or len(str2) == 0:
        return 0.0

    rows = len(str1) + 1
    cols = len(str2) + 1
    dist = [[0 for _ in range(cols)] for _ in range(rows)]

    for i in range(1, rows):
        dist[i][0] = i
    for i in range(1, cols):
        dist[0][i] = i

    for row in range(1, rows):
        for col in range(1, cols):
            cost = 0 if str1[row - 1] == str2[col - 1] else 1
            dist[row][col] = min(
                dist[row - 1][col] + 1,
                dist[row][col - 1] + 1,
                dist[row - 1][col - 1] + cost,
            )

    return 1 - (dist[rows - 1][cols - 1] / max(len(str1), len(str2)))


def _clean_company_name(name: str) -> str:
    """Normalize company name for comparison."""
    if not name:
        return ""

    # Remove common suffixes
    name = re.sub(r'\s+(Inc|LLC|Ltd|Limited|Corporation|Corp|Co|GmbH|AG|SA|S\.A\.R\.L|SARL)\s*\.?$', '', name, flags=re.IGNORECASE)
    # Remove whitespace
    name = ' '.join(name.split())
    return name.lower().strip()


class ManufacturerLookup:
    """Find manufacturer info from multiple sources."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'PharmaSearch/1.0 (Educational Research)'
        })

    def lookup(
        self,
        product_name: str,
        company_name: str,
        substance: str = "",
        country: str = "",
    ) -> dict:
        """Look up manufacturer info from multiple sources.

        Returns:
            {
                'manufacturer': 'Name',
                'country': 'Country',
                'address': 'Address',
                'source': 'DrugBank',
                'confidence': 0.85,
                'url': 'https://...',
                'raw_data': {...}
            }
        """
        results = []

        # Try multiple sources
        logger.info(f"Looking up: {product_name} from {company_name}")

        # 1. Try DrugBank
        db_result = self._lookup_drugbank(product_name, substance)
        if db_result:
            results.append(db_result)

        # 2. Try OpenFDA (USA only)
        if country in ['USA', 'United States', 'US']:
            fda_result = self._lookup_openfda(product_name)
            if fda_result:
                results.append(fda_result)

        # 3. Try company website
        web_result = self._lookup_company_website(company_name)
        if web_result:
            results.append(web_result)

        # Return best match
        if results:
            best = max(results, key=lambda x: x.get('confidence', 0))
            return best

        return None

    def _lookup_drugbank(self, product_name: str, substance: str) -> Optional[dict]:
        """Look up in DrugBank."""
        try:
            logger.debug(f"Searching DrugBank for: {product_name}")

            # DrugBank doesn't have public API, so this is a placeholder
            # In production, you'd scrape or use their XML download
            return None

        except Exception as e:
            logger.warning(f"DrugBank lookup failed: {e}")
            return None

    def _lookup_openfda(self, product_name: str) -> Optional[dict]:
        """Look up in OpenFDA API."""
        try:
            logger.debug(f"Searching OpenFDA for: {product_name}")

            url = SOURCES['openfda']['url']
            params = {
                'search': f'brand_name:"{product_name}"',
                'limit': 5,
            }

            response = self.session.get(url, params=params, timeout=SEARCH_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            if data.get('results'):
                for result in data['results']:
                    manufacturer = result.get('manufacturer_name', [''])[0] if isinstance(result.get('manufacturer_name'), list) else result.get('manufacturer_name')

                    if manufacturer:
                        return {
                            'manufacturer': manufacturer,
                            'country': 'USA',
                            'source': 'OpenFDA',
                            'confidence': 0.9,
                            'url': 'https://open.fda.gov',
                            'raw_data': result,
                        }

            return None

        except Exception as e:
            logger.warning(f"OpenFDA lookup failed: {e}")
            return None

    def _lookup_company_website(self, company_name: str) -> Optional[dict]:
        """Try to find company website and scrape manufacturer info.

        This is a simplified version. Real implementation would:
        - Search for company website
        - Scrape for manufacturing/facilities information
        - Extract structured data
        """
        try:
            logger.debug(f"Searching company website for: {company_name}")

            # Placeholder - would need real web scraping
            return None

        except Exception as e:
            logger.warning(f"Company website lookup failed: {e}")
            return None

    def validate(
        self,
        manufacturer_name: str,
        licence_holder: str,
    ) -> dict:
        """Validate manufacturer matches licence holder.

        Returns:
            {
                'is_valid': bool,
                'match_score': 0.85,
                'reason': 'Direct match' or 'Same company family',
            }
        """
        if not manufacturer_name or not licence_holder:
            return {
                'is_valid': False,
                'match_score': 0,
                'reason': 'Missing data',
            }

        cleaned_mfg = _clean_company_name(manufacturer_name)
        cleaned_lh = _clean_company_name(licence_holder)

        score = _similarity_score(cleaned_mfg, cleaned_lh)

        # Decision logic
        if score > 0.95:
            return {
                'is_valid': True,
                'match_score': score,
                'reason': 'Direct match',
            }
        elif score > 0.8:
            return {
                'is_valid': True,
                'match_score': score,
                'reason': 'Strong match',
            }
        elif score > 0.7:
            return {
                'is_valid': True,
                'match_score': score,
                'reason': 'Possible match - needs review',
            }
        else:
            return {
                'is_valid': False,
                'match_score': score,
                'reason': 'Low match score',
            }


def find_manufacturer(
    product_name: str,
    company_name: str,
    substance: str = "",
    country: str = "",
) -> Optional[dict]:
    """Main entry point for manufacturer lookup.

    Returns result or None if not found/not confident.
    """
    lookup = ManufacturerLookup()
    result = lookup.lookup(product_name, company_name, substance, country)

    if not result:
        return None

    # Validate against licence holder
    validation = lookup.validate(result['manufacturer'], company_name)
    result['validation'] = validation

    # Only return if confident enough
    if result.get('confidence', 0) >= CONFIDENCE_THRESHOLD:
        return result

    return None
