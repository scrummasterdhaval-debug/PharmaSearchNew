import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
DELETE FROM product_details
WHERE id IN (
    10,
    11,
    12,
    13,
    14,
    15,
    16
)
""")

deleted_count = cursor.rowcount

conn.commit()

conn.close()

print(f"{deleted_count} bad records deleted successfully")
