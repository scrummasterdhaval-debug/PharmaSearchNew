import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
UPDATE product_details
SET country='Sweden'
WHERE company='AstraZeneca AB'
""")

cursor.execute("""
UPDATE product_details
SET country='Germany'
WHERE company='Boehringer Ingelheim International GmbH'
""")

cursor.execute("""
UPDATE product_details
SET country='United Kingdom'
WHERE region='UK'
""")

conn.commit()

cursor.execute("""
SELECT product, company, country, region
FROM product_details
""")

for row in cursor.fetchall():
    print(row)

conn.close()
