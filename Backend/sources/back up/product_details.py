import sqlite3

def save_product_details(
    substance,
    product,
    company,
    country,
    status,
    product_url="",
    atc_code="",
    registration_date=""
):

    conn = sqlite3.connect("pharmasearch.db")

    cursor = conn.cursor()

    # Check if product already exists
    cursor.execute("""
        SELECT id
        FROM product_details
        WHERE product = ?
    """, (product,))

    existing = cursor.fetchone()

    if existing:

        conn.close()

        print("Product already exists")

        return

    # Insert new product
    cursor.execute
