"""Admin task for manufacturer lookup and validation.

Finds missing manufacturer data and stores suggestions for admin review.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from repository import get_connection, initialize_database
from core.logging_config import get_logger
from services.manufacturer_lookup import find_manufacturer

logger = get_logger(__name__)


def get_products_needing_manufacturer(limit: int = 100) -> list[dict]:
    """Get products missing manufacturer data.

    Returns products in priority order:
    1. Large regulators (MHRA, EMA, FDA first)
    2. Most commonly used products first
    """
    initialize_database()

    query = """
        SELECT
            id, product, company, substance, country, source,
            registration_number, strength, dosage_form
        FROM product_details
        WHERE (manufacturer_name IS NULL OR manufacturer_name = '')
        AND company IS NOT NULL
        AND company != ''
        AND product IS NOT NULL
        AND product != ''
        ORDER BY
            CASE
                WHEN source IN ('MHRA', 'EMA', 'FDA', 'Health Canada') THEN 0
                WHEN source IN ('France BDPM', 'Spain CIMA') THEN 1
                ELSE 2
            END,
            product
        LIMIT ?
    """

    with get_connection() as conn:
        rows = conn.execute(query, (limit,)).fetchall()

        return [
            {
                'id': row[0],
                'product': row[1],
                'company': row[2],
                'substance': row[3],
                'country': row[4],
                'source': row[5],
                'registration_number': row[6],
                'strength': row[7],
                'dosage_form': row[8],
            }
            for row in rows
        ]


def save_manufacturer_suggestion(
    product_id: int,
    suggestion: dict,
    status: str = 'pending',
) -> bool:
    """Save a manufacturer suggestion for admin review.

    Args:
        product_id: ID of product
        suggestion: {
            'manufacturer': str,
            'country': str,
            'address': str (optional),
            'source': str,
            'confidence': float,
            'url': str (optional),
            'validation': {...}
        }
        status: 'pending', 'approved', 'rejected'
    """
    initialize_database()

    try:
        with get_connection() as conn:
            # Check if suggestion already exists
            existing = conn.execute(
                """
                SELECT id FROM manufacturer_suggestions
                WHERE product_id = ? AND source = ? AND status != 'rejected'
                """,
                (product_id, suggestion.get('source'))
            ).fetchone()

            if existing:
                logger.debug(f"Suggestion already exists for product {product_id}")
                return False

            # Save suggestion
            conn.execute(
                """
                INSERT INTO manufacturer_suggestions
                (product_id, manufacturer_name, manufacturer_country,
                 manufacturer_address, source_url, confidence_score,
                 suggestion_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    suggestion.get('manufacturer'),
                    suggestion.get('country'),
                    suggestion.get('address', ''),
                    suggestion.get('url', ''),
                    suggestion.get('confidence', 0),
                    json.dumps(suggestion),
                    status,
                    datetime.now(timezone.utc).isoformat(),
                )
            )

            logger.info(f"Saved suggestion for product {product_id}: {suggestion.get('manufacturer')}")
            return True

    except Exception as e:
        logger.error(f"Failed to save suggestion: {e}")
        return False


def run_manufacturer_lookup(batch_size: int = 50) -> dict:
    """Run manufacturer lookup on missing products.

    Returns stats: {
        'processed': int,
        'found': int,
        'saved': int,
        'skipped': int,
        'errors': int,
    }
    """
    stats = {
        'processed': 0,
        'found': 0,
        'saved': 0,
        'skipped': 0,
        'errors': 0,
    }

    # Get products needing manufacturer
    products = get_products_needing_manufacturer(limit=batch_size)
    logger.info(f"Starting manufacturer lookup for {len(products)} products")

    for product in products:
        stats['processed'] += 1

        try:
            # Look up manufacturer
            result = find_manufacturer(
                product_name=product['product'],
                company_name=product['company'],
                substance=product.get('substance', ''),
                country=product.get('country', ''),
            )

            if result:
                stats['found'] += 1

                # Only save if validation passed
                if result.get('validation', {}).get('is_valid'):
                    if save_manufacturer_suggestion(product['id'], result):
                        stats['saved'] += 1
                    else:
                        stats['skipped'] += 1
                else:
                    stats['skipped'] += 1
            else:
                stats['skipped'] += 1

        except Exception as e:
            logger.error(f"Error looking up product {product['id']}: {e}")
            stats['errors'] += 1

    logger.info(f"Lookup complete: {stats}")
    return stats


def get_pending_suggestions(limit: int = 50) -> list[dict]:
    """Get pending manufacturer suggestions for admin review."""
    initialize_database()

    query = """
        SELECT
            ms.id, ms.product_id, pd.product, pd.company,
            ms.manufacturer_name, ms.manufacturer_country,
            ms.confidence_score, ms.suggestion_json, pd.source,
            ms.created_at
        FROM manufacturer_suggestions ms
        JOIN product_details pd ON ms.product_id = pd.id
        WHERE ms.status = 'pending'
        ORDER BY ms.confidence_score DESC, ms.created_at ASC
        LIMIT ?
    """

    with get_connection() as conn:
        rows = conn.execute(query, (limit,)).fetchall()

        return [
            {
                'suggestion_id': row[0],
                'product_id': row[1],
                'product_name': row[2],
                'licence_holder': row[3],
                'manufacturer_name': row[4],
                'manufacturer_country': row[5],
                'confidence_score': row[6],
                'suggestion_data': json.loads(row[7]) if row[7] else {},
                'source': row[8],
                'created_at': row[9],
            }
            for row in rows
        ]


def approve_suggestion(suggestion_id: int, admin_email: str = "") -> bool:
    """Admin approves a suggestion - apply it to product."""
    initialize_database()

    try:
        with get_connection() as conn:
            # Get suggestion
            suggestion = conn.execute(
                "SELECT product_id, manufacturer_name, manufacturer_country, manufacturer_address, suggestion_json FROM manufacturer_suggestions WHERE id = ?",
                (suggestion_id,)
            ).fetchone()

            if not suggestion:
                return False

            product_id, mfg_name, mfg_country, mfg_addr, suggestion_json = suggestion

            # Update product
            conn.execute(
                """
                UPDATE product_details
                SET manufacturer_name = ?, manufacturer_country = ?,
                    manufacturer_address = ?, manufacturer_source = ?,
                    last_checked = ?
                WHERE id = ?
                """,
                (
                    mfg_name,
                    mfg_country,
                    mfg_addr,
                    f"Automated lookup ({json.loads(suggestion_json).get('source', 'Unknown')})",
                    datetime.now(timezone.utc).isoformat(),
                    product_id,
                )
            )

            # Mark suggestion as approved
            conn.execute(
                "UPDATE manufacturer_suggestions SET status = 'approved', reviewed_at = ?, reviewed_by = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), admin_email, suggestion_id)
            )

            logger.info(f"Approved suggestion {suggestion_id} for product {product_id}")
            return True

    except Exception as e:
        logger.error(f"Failed to approve suggestion: {e}")
        return False


def reject_suggestion(suggestion_id: int, admin_email: str = "", reason: str = "") -> bool:
    """Admin rejects a suggestion."""
    initialize_database()

    try:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE manufacturer_suggestions
                SET status = 'rejected', reviewed_at = ?, reviewed_by = ?,
                    rejection_reason = ?
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), admin_email, reason, suggestion_id)
            )

            logger.info(f"Rejected suggestion {suggestion_id}: {reason}")
            return True

    except Exception as e:
        logger.error(f"Failed to reject suggestion: {e}")
        return False
