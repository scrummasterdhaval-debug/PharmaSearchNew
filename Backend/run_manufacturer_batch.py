#!/usr/bin/env python3
"""
Run comprehensive manufacturer data fill on medicines table.
Fills missing manufacturers using PubChem, FDA NDC, EMA, and other sources.
"""

import sys
import logging
from datetime import datetime
from repository import get_connection
from services.comprehensive_manufacturer_scraper import ComprehensiveManufacturerScraper
from services.regulatory_pdf_parser import RegulatoryPDFParser
import json
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class MedicineManufacturerFill:
    """Fill manufacturers for medicines with missing data."""

    def __init__(self):
        self.scraper = ComprehensiveManufacturerScraper()
        self.pdf_parser = RegulatoryPDFParser()

    def get_medicines_without_manufacturer(self, limit: int = None):
        """Get medicines with null/empty company field."""
        with get_connection() as conn:
            cursor = conn.cursor()
            query = """
                SELECT id, product, substance, country
                FROM medicines
                WHERE (company IS NULL OR TRIM(company) = '' OR company = 'UNKNOWN')
                  AND product IS NOT NULL
                  AND LENGTH(TRIM(product)) > 2
                ORDER BY product ASC
            """
            if limit:
                query += f" LIMIT {limit}"

            cursor.execute(query)
            return cursor.fetchall()

    def find_manufacturer(self, product_name, substance, country):
        """Find manufacturer from web + PDF sources."""
        web_result = self.scraper.find_manufacturer_comprehensive(
            product_name=product_name,
            substance=substance,
            country=country
        )
        time.sleep(0.3)

        pdf_result = self.pdf_parser.extract_from_all_regulatory_pdfs(
            product_name=product_name,
            country=country
        )
        time.sleep(0.3)

        # Pick highest confidence result
        web_conf = web_result.get('confidence', 0) if web_result else 0
        pdf_conf = pdf_result.get('confidence', 0) if pdf_result else 0

        if pdf_conf > web_conf:
            best = pdf_result
        else:
            best = web_result

        return best

    def store_suggestion(self, product_id, manufacturer_name, source_url, confidence, country=''):
        """Store manufacturer suggestion."""
        with get_connection() as conn:
            cursor = conn.cursor()

            # Determine status
            if confidence >= 0.85:
                status = 'auto_approved'
            elif confidence >= 0.75:
                status = 'pending_review_high'
            else:
                status = 'pending_review'

            cursor.execute("""
                INSERT INTO manufacturer_suggestions
                (product_id, manufacturer_name, manufacturer_country, source_url, confidence_score, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                product_id,
                manufacturer_name,
                country,
                source_url,
                confidence,
                status,
                datetime.now().isoformat()
            ))
            conn.commit()

    def run(self, limit=None, test_mode=False):
        """Run batch fill on medicines."""
        logger.info("=" * 70)
        logger.info(f"MANUFACTURER BATCH FILL - {'TEST MODE' if test_mode else 'LIVE MODE'}")
        logger.info("=" * 70)

        medicines = self.get_medicines_without_manufacturer(limit)

        if not medicines:
            logger.warning("No medicines found with missing manufacturers")
            return {
                'total': 0,
                'processed': 0,
                'filled': 0,
                'status': 'no_data',
            }

        stats = {
            'total': len(medicines),
            'processed': 0,
            'filled': 0,
            'auto_approved': 0,
            'pending_high': 0,
            'pending_review': 0,
            'failed': 0,
            'sources_used': {},
        }

        logger.info(f"Processing {len(medicines)} medicines...")

        for idx, medicine in enumerate(medicines):
            if idx % 10 == 0:
                logger.info(f"Progress: {idx}/{len(medicines)}")

            med_id, product_name, substance, country = medicine

            try:
                result = self.find_manufacturer(product_name, substance, country or '')

                if result and result.get('manufacturer'):
                    confidence = result.get('confidence', 0)
                    source = result.get('source', 'Unknown')
                    url = result.get('url', '')

                    stats['sources_used'][source] = stats['sources_used'].get(source, 0) + 1

                    # Store if meets threshold
                    if confidence >= 0.65 and not test_mode:
                        self.store_suggestion(med_id, result['manufacturer'], url, confidence, country or '')

                    stats['filled'] += 1

                    if confidence >= 0.85:
                        stats['auto_approved'] += 1
                    elif confidence >= 0.75:
                        stats['pending_high'] += 1
                    else:
                        stats['pending_review'] += 1
                else:
                    stats['failed'] += 1

            except Exception as e:
                logger.debug(f"Error processing medicine {med_id}: {e}")
                stats['failed'] += 1

            stats['processed'] += 1
            time.sleep(0.5)  # Rate limiting

        # Summary
        logger.info("=" * 70)
        logger.info("BATCH COMPLETE")
        logger.info("=" * 70)
        logger.info(f"Total:        {stats['total']}")
        logger.info(f"Processed:    {stats['processed']}")
        logger.info(f"Filled:       {stats['filled']} ({100*stats['filled']/stats['processed']:.1f}%)")
        logger.info(f"  Auto-approved (90%+):  {stats['auto_approved']}")
        logger.info(f"  Pending (high):        {stats['pending_high']}")
        logger.info(f"  Pending (medium):      {stats['pending_review']}")
        logger.info(f"  Failed:                {stats['failed']}")
        logger.info("")
        logger.info("Sources used:")
        for source, count in sorted(stats['sources_used'].items(), key=lambda x: x[1], reverse=True):
            logger.info(f"  {source}: {count}")
        logger.info("=" * 70)

        return stats

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Fill manufacturers for medicines')
    parser.add_argument('--limit', type=int, default=None, help='Max medicines to process')
    parser.add_argument('--test', action='store_true', help='Test mode (no DB writes)')
    args = parser.parse_args()

    filler = MedicineManufacturerFill()
    stats = filler.run(limit=args.limit, test_mode=args.test)

    sys.exit(0 if stats['filled'] > 0 else 1)
