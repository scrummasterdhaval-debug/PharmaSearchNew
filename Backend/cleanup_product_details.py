import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
DELETE FROM product_details
WHERE product NOT IN (
    'Forxiga',
    'Jardiance'
)
""")

deleted_count = cursor.rowcount

conn.commit()

conn.close()

print(f"{deleted_count} records deleted")
