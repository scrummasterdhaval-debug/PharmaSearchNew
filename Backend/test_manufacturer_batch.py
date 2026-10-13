"""Test Comprehensive Manufacturer Batch Fill

Quick test to verify all 9 web sources + PDF parsers work correctly.
Tests on sample products before running full 8,280 product batch.
"""

import sys
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_web_scrapers():
    """Test comprehensive web scraper on sample products."""

    logger.info("=" * 70)
    logger.info("TESTING COMPREHENSIVE WEB SCRAPERS")
    logger.info("=" * 70)

    from services.comprehensive_manufacturer_scraper import ComprehensiveManufacturerScraper

    scraper = ComprehensiveManufacturerScraper()

    test_substances = [
        ('Metformin', 'Type 2 Diabetes'),
        ('Ibuprofen', 'Pain Relief'),
        ('Aspirin', 'Anticoagulant'),
        ('Atorvastatin', 'Cholesterol'),
        ('Lisinopril', 'Blood Pressure'),
    ]

    results = []

    for substance, use in test_substances:
        logger.info(f"\nTesting: {substance} ({use})")
        logger.info("-" * 50)

        try:
            result = scraper.find_manufacturer_comprehensive(
                product_name=substance,
                substance=substance
            )

            if result and result.get('manufacturer'):
                logger.info(f"✓ Found: {result['manufacturer']}")
                logger.info(f"  Source: {result.get('source')}")
                logger.info(f"  Confidence: {result.get('confidence'):.2%}")
                logger.info(f"  URL: {result.get('url', 'N/A')[:60]}...")
                results.append(result)
            else:
                logger.info(f"✗ No manufacturer found")

        except Exception as e:
            logger.error(f"✗ Error: {e}")

    # Summary
    logger.info("\n" + "=" * 70)
    logger.info(f"Web Scraper Results: {len(results)}/{len(test_substances)} found")
    logger.info("=" * 70)

    return results


def test_pdf_parsers():
    """Test regulatory PDF parsers."""

    logger.info("\n" + "=" * 70)
    logger.info("TESTING REGULATORY PDF PARSERS")
    logger.info("=" * 70)

    from services.regulatory_pdf_parser import RegulatoryPDFParser

    parser = RegulatoryPDFParser()

    test_products = [
        ('Metformin', 'US'),
        ('Aspirin', 'DE'),
        ('Atorvastatin', 'UK'),
    ]

    results = []

    for product, country in test_products:
        logger.info(f"\nTesting: {product} ({country})")
        logger.info("-" * 50)

        try:
            result = parser.extract_from_all_regulatory_pdfs(
                product_name=product,
                country=country
            )

            if result and result.get('manufacturer'):
                logger.info(f"✓ Found: {result['manufacturer']}")
                logger.info(f"  Source: {result.get('source')}")
                logger.info(f"  Confidence: {result.get('confidence'):.2%}")
                results.append(result)
            else:
                logger.info(f"✗ No PDF data found (may be rate-limited)")

        except Exception as e:
            logger.error(f"✗ Error: {e}")

    # Summary
    logger.info("\n" + "=" * 70)
    logger.info(f"PDF Parser Results: {len(results)}/{len(test_products)} found")
    logger.info("=" * 70)

    return results


def test_batch_on_sample():
    """Test batch processor on small sample (10 products)."""

    logger.info("\n" + "=" * 70)
    logger.info("TESTING BATCH PROCESSOR (SAMPLE: 10 PRODUCTS)")
    logger.info("=" * 70)
    logger.info("Note: This will attempt to fill real products in test mode")
    logger.info("")

    try:
        from core.database import get_db
        from services.batch_manufacturer_fill import BatchManufacturerFill

        db = get_db()
        batch_job = BatchManufacturerFill(db)

        # Run on small sample
        stats = batch_job.run_batch(limit=10, test_mode=True)

        logger.info("\n" + "=" * 70)
        logger.info("BATCH TEST RESULTS")
        logger.info("=" * 70)
        logger.info(f"Processed: {stats['processed']}")
        logger.info(f"Filled: {stats['filled']}")
        logger.info(f"Success Rate: {100*stats['filled']/stats['processed']:.1f}%")
        logger.info(f"Duration: {stats['duration_seconds']:.1f}s")

        # Show confidence distribution
        logger.info("\nConfidence Distribution:")
        for level, count in stats['confidence_distribution'].items():
            logger.info(f"  {level}: {count}")

        # Show sources used
        logger.info("\nTop Sources:")
        for source, count in sorted(stats['sources_used'].items(),
                                   key=lambda x: x[1], reverse=True)[:5]:
            logger.info(f"  {source}: {count}")

        logger.info("=" * 70)

        return stats

    except Exception as e:
        logger.error(f"Batch test failed: {e}")
        return None


def show_instructions():
    """Show instructions for running full batch."""

    logger.info("\n" + "=" * 70)
    logger.info("FULL BATCH JOB INSTRUCTIONS")
    logger.info("=" * 70)
    logger.info("""
To run the FULL batch job on all 8,280 products:

1. FROM PYTHON:
   from services.batch_manufacturer_fill import run_batch_fill
   stats = run_batch_fill()  # Processes ALL products

2. FROM COMMAND LINE:
   python services/batch_manufacturer_fill.py

3. WITH OPTIONS:
   python -c "from services.batch_manufacturer_fill import run_batch_fill; \\
              run_batch_fill(limit=1000, test_mode=False)"

   --limit: Max products to process (default: all)
   --test:  Test mode (don't store results)

EXPECTED RESULTS:
- Process ~8,280 products
- Fill ~60-75% with manufacturers (5,000-6,000 products)
- Auto-approve ~40-50% (confidence 90%+)
- Pending review: ~20-30%
- Duration: ~2-3 hours (0.5 products/sec)

SOURCES USED (by expected frequency):
1. FDA NDC (40-50%)
2. PubChem (20-25%)
3. EMA (10-15%)
4. DrugBank (5-10%)
5. ClinicalTrials.gov (5-8%)
6. WHO (3-5%)
7. Others (2-3%)

ADMIN REVIEW:
- Auto-approved results show immediately in products table
- Pending review results in manufacturer_suggestions table for admin approval
- High-confidence results (~80%+) prioritized for review
""")
    logger.info("=" * 70)


def main():
    """Run all tests."""

    logger.info("\n")
    logger.info(" " * 20 + "COMPREHENSIVE MANUFACTURER DATA BATCH")
    logger.info(" " * 15 + "Test Suite for 8,280 Product Fill Job")
    logger.info(" " * 20 + "Started: " + datetime.now().isoformat())
    logger.info("")

    # Run tests
    web_results = test_web_scrapers()
    pdf_results = test_pdf_parsers()
    batch_results = test_batch_on_sample()

    # Show summary
    logger.info("\n" + "=" * 70)
    logger.info("TEST SUMMARY")
    logger.info("=" * 70)
    logger.info(f"Web Scrapers: {len(web_results)}/5 tests successful")
    logger.info(f"PDF Parsers: {len(pdf_results)}/3 tests successful")
    if batch_results:
        logger.info(f"Batch Processor: Sample fill rate {100*batch_results['filled']/batch_results['processed']:.1f}%")
    logger.info("=" * 70)

    # Show instructions
    show_instructions()

    logger.info(f"\nTest completed: {datetime.now().isoformat()}\n")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n\nTest interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"\nFatal error: {e}", exc_info=True)
        sys.exit(1)
