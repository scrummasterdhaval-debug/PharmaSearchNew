import os
import sqlite3

print(os.path.abspath("pharmasearch.db"))

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
SELECT *
FROM product_details
""")

rows = cursor.fetchall()

for row in rows:
    print(row)

conn.close()
