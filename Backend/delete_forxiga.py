import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
DELETE FROM product_details
WHERE product = 'Forxiga'
""")

conn.commit()

conn.close()

print("Forxiga deleted")
