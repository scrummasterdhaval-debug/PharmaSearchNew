"""Batch Manufacturer Data Fill Job

Processes all 8,280 products with missing manufacturers using:
- Comprehensive web scrapers (9 sources)
- Regulatory PDF parsers (EMA, FDA, WHO, National)
- Confidence scoring and deduplication
- Admin review workflow

Usage:
    python -c "from services.batch_manufacturer_fill import run_batch_fill; run_batch_fill(limit=100)"
"""

import logging
import time
from datetime import datetime
from typing import Optional, List, Dict
import json

logger = logging.getLogger(__name__)

class BatchManufacturerFill:
    """Batch process manufacturer data for all products."""

    def __init__(self, db):
        self.db = db
        from services.comprehensive_manufacturer_scraper import ComprehensiveManufacturerScraper
        from services.regulatory_pdf_parser import RegulatoryPDFParser, combine_web_and_pdf_results

        self.scraper = ComprehensiveManufacturerScraper()
        self.pdf_parser = RegulatoryPDFParser()
        self.combine_results = combine_web_and_pdf_results

    def get_products_without_manufacturer(self, limit: int = None) -> List[Dict]:
        """Get all products missing manufacturer data."""
        try:
            # Query medicines table for null/empty manufacturer
            query = """
                SELECT
                    id,
                    name as product_name,
                    substance,
                    country,
                    substance as active_ingredient,
                    'medicine' as product_type
                FROM medicines
                WHERE
                    (manufacturer IS NULL OR manufacturer = '' OR manufacturer = 'UNKNOWN')
                    AND name IS NOT NULL
                    AND LENGTH(TRIM(name)) > 0
                ORDER BY name ASC
            """

            if limit:
                query += f" LIMIT {limit}"

            cursor = self.db.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            results = [dict(row) for row in rows] if rows else []
            cursor.close()
            return results

        except Exception as e:
            logger.error(f"Error fetching products without manufacturer: {e}")
            return []

    def process_single_product(self, product: Dict) -> Optional[Dict]:
        """Process one product through all data sources."""

        product_id = product.get('id')
        product_name = product.get('product_name', '').strip()
        substance = product.get('substance', '').strip()
        country = product.get('country', '').strip()

        if not product_name:
            return None

        try:
            # 1. Try comprehensive web scrapers
            web_result = self.scraper.find_manufacturer_comprehensive(
                product_name=product_name,
                substance=substance,
                country=country
            )

            # Small delay to avoid rate limiting
            time.sleep(0.3)

            # 2. Try regulatory PDF parsers
            pdf_result = self.pdf_parser.extract_from_all_regulatory_pdfs(
                product_name=product_name,
                country=country
            )

            # Small delay
            time.sleep(0.3)

            # 3. Combine results (prefer highest confidence)
            if web_result.get('manufacturer') or pdf_result.get('manufacturer'):
                final_result = self.combine_results(web_result, pdf_result)
            else:
                return None

            # Add product metadata
            final_result['product_id'] = product_id
            final_result['product_name'] = product_name
            final_result['substance'] = substance
            final_result['country'] = country
            final_result['processed_at'] = datetime.now().isoformat()

            return final_result

        except Exception as e:
            logger.error(f"Error processing product {product_id} ({product_name}): {e}")
            return None

    def store_result(self, result: Dict) -> bool:
        """Store manufacturer suggestion in database."""
        try:
            confidence = result.get('highest_confidence', result.get('confidence', 0))

            # Only store if meets minimum threshold
            if confidence < 0.65:
                return False

            # Determine status based on confidence
            if confidence >= 0.85:
                status = 'auto_approved'  # High confidence - auto approve
            elif confidence >= 0.75:
                status = 'pending_review_high'  # Medium-high - prioritize review
            else:
                status = 'pending_review'  # Needs review

            # Store in manufacturer_suggestions table
            insert_query = """
                INSERT INTO manufacturer_suggestions
                (product_id, manufacturer, source, confidence, url, status, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """

            notes = json.dumps({
                'combined_from': result.get('combined_from'),
                'sources_tried': len(result.get('all_sources', [])),
                'pdf_sources_tried': len(result.get('all_pdf_sources', [])),
                'web_method': result.get('all_sources', [{}])[0].get('source') if result.get('all_sources') else None,
                'pdf_method': result.get('all_pdf_sources', [{}])[0].get('source') if result.get('all_pdf_sources') else None,
            })

            cursor = self.db.cursor()
            cursor.execute(insert_query, (
                result['product_id'],
                result.get('manufacturer'),
                result.get('source', 'Combined'),
                confidence,
                result.get('url', ''),
                status,
                notes,
                datetime.now().isoformat()
            ))
            self.db.commit()
            cursor.close()

            return True

        except Exception as e:
            logger.error(f"Error storing result for product {result.get('product_id')}: {e}")
            return False

    def run_batch(self, limit: int = None, test_mode: bool = False) -> Dict:
        """
        Run batch fill job.

        Args:
            limit: Max products to process (None = all)
            test_mode: If True, don't store results, just collect stats

        Returns:
            Processing statistics
        """

        logger.info("=" * 70)
        logger.info("BATCH MANUFACTURER FILL JOB STARTED")
        logger.info("=" * 70)

        # Get products to process
        products = self.get_products_without_manufacturer(limit=limit)

        if not products:
            logger.warning("No products found without manufacturer data")
            return {
                'total_products': 0,
                'processed': 0,
                'filled': 0,
                'status': 'no_products',
            }

        # Processing statistics
        stats = {
            'start_time': datetime.now().isoformat(),
            'total_products': len(products),
            'processed': 0,
            'filled': 0,
            'auto_approved': 0,
            'pending_high': 0,
            'pending_review': 0,
            'below_threshold': 0,
            'failed': 0,
            'sources_used': {},
            'confidence_distribution': {
                '0.90+': 0,
                '0.80-0.89': 0,
                '0.70-0.79': 0,
                '0.65-0.69': 0,
                '<0.65': 0,
            },
            'test_mode': test_mode,
        }

        # Process each product
        for idx, product in enumerate(products):
            # Log progress
            if idx % 50 == 0:
                elapsed = (datetime.now() - datetime.fromisoformat(stats['start_time'])).total_seconds()
                rate = (idx / elapsed) if elapsed > 0 else 0
                logger.info(f"Progress: {idx}/{len(products)} ({100*idx/len(products):.1f}%) | "
                           f"Rate: {rate:.1f} products/sec | Filled: {stats['filled']}")

            try:
                # Process product
                result = self.process_single_product(product)
                stats['processed'] += 1

                if result and result.get('manufacturer'):
                    confidence = result.get('highest_confidence', 0)
                    source = result.get('source', 'Unknown')

                    # Track source usage
                    stats['sources_used'][source] = stats['sources_used'].get(source, 0) + 1

                    # Track confidence distribution
                    if confidence >= 0.90:
                        stats['confidence_distribution']['0.90+'] += 1
                        stats['auto_approved'] += 1
                    elif confidence >= 0.80:
                        stats['confidence_distribution']['0.80-0.89'] += 1
                        stats['pending_high'] += 1
                    elif confidence >= 0.70:
                        stats['confidence_distribution']['0.70-0.79'] += 1
                        stats['pending_review'] += 1
                    elif confidence >= 0.65:
                        stats['confidence_distribution']['0.65-0.69'] += 1
                        stats['pending_review'] += 1
                    else:
                        stats['confidence_distribution']['<0.65'] += 1
                        stats['below_threshold'] += 1
                        continue  # Don't store below-threshold results

                    # Store result (unless test mode)
                    if not test_mode:
                        if self.store_result(result):
                            stats['filled'] += 1
                    else:
                        stats['filled'] += 1
                else:
                    stats['failed'] += 1

            except Exception as e:
                logger.error(f"Error processing product {idx}: {e}")
                stats['failed'] += 1

            # Be respectful to remote servers
            time.sleep(0.5)

        # Final statistics
        stats['end_time'] = datetime.now().isoformat()
        total_seconds = (datetime.fromisoformat(stats['end_time']) -
                        datetime.fromisoformat(stats['start_time'])).total_seconds()
        stats['duration_seconds'] = total_seconds
        stats['average_rate'] = stats['processed'] / total_seconds if total_seconds > 0 else 0

        # Log results
        logger.info("=" * 70)
        logger.info("BATCH JOB COMPLETE")
        logger.info("=" * 70)
        logger.info(f"Total Products: {stats['total_products']}")
        logger.info(f"Processed: {stats['processed']}")
        logger.info(f"Filled: {stats['filled']} ({100*stats['filled']/stats['processed']:.1f}%)")
        logger.info(f"  - Auto-approved (90%+): {stats['auto_approved']}")
        logger.info(f"  - Pending review (high): {stats['pending_high']}")
        logger.info(f"  - Pending review (medium): {stats['pending_review']}")
        logger.info(f"  - Below threshold: {stats['below_threshold']}")
        logger.info(f"Failed: {stats['failed']}")
        logger.info(f"Duration: {total_seconds:.1f} seconds ({total_seconds/60:.1f} minutes)")
        logger.info(f"Average Rate: {stats['average_rate']:.1f} products/second")
        logger.info("")
        logger.info("Top Sources Used:")
        for source, count in sorted(stats['sources_used'].items(), key=lambda x: x[1], reverse=True)[:10]:
            logger.info(f"  - {source}: {count} products")
        logger.info("")
        logger.info("Confidence Distribution:")
        for level, count in stats['confidence_distribution'].items():
            logger.info(f"  - {level}: {count}")
        logger.info("=" * 70)

        return stats


# ==================== COMMAND-LINE INTERFACE ====================

def run_batch_fill(db=None, limit: int = None, test_mode: bool = False):
    """Run batch fill from command line or programmatically."""

    if db is None:
        # Import database connection
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).parent.parent / 'pharmadb.sqlite'
        db = sqlite3.connect(str(db_path), timeout=30)
        db.row_factory = sqlite3.Row

    batch_job = BatchManufacturerFill(db)
    return batch_job.run_batch(limit=limit, test_mode=test_mode)


if __name__ == '__main__':
    import sys

    # Parse arguments
    limit = None
    test_mode = False

    if '--limit' in sys.argv:
        idx = sys.argv.index('--limit')
        limit = int(sys.argv[idx + 1])

    if '--test' in sys.argv:
        test_mode = True

    # Run
    stats = run_batch_fill(limit=limit, test_mode=test_mode)

    # Print summary
    print("\n" + "=" * 70)
    print("BATCH JOB SUMMARY")
    print("=" * 70)
    print(f"Filled: {stats['filled']}/{stats['total_products']} products")
    print(f"Success Rate: {100*stats['filled']/stats['total_products']:.1f}%")
    print(f"Duration: {stats['duration_seconds']:.1f} seconds")
    print("=" * 70)
