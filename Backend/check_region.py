import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
SELECT product, country, region
FROM product_details
""")

for row in cursor.fetchall():
    print(row)

conn.close()
