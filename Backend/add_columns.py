import sqlite3

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

cursor.execute("""
ALTER TABLE product_details
ADD COLUMN atc_code TEXT
""")

cursor.execute("""
ALTER TABLE product_details
ADD COLUMN registration_date TEXT
""")

conn.commit()
conn.close()

print("Columns added")
