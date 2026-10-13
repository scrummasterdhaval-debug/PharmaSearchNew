import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
UPDATE product_details
SET region = country
""")

conn.commit()

print("Rows updated:", cursor.rowcount)

conn.close()
