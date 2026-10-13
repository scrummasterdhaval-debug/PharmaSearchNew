import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
DELETE FROM product_details
WHERE id IN (1,2)
""")

conn.commit()

conn.close()

print("Old rows removed")
