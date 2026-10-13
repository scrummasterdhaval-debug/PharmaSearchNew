import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
DELETE FROM product_details
WHERE id IN (5,6)
""")

conn.commit()

conn.close()

print("Duplicates removed")
