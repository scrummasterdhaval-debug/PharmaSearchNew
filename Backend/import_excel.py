import pandas as pd
import sqlite3

df = pd.read_excel("data/sample_medicines.xlsx")

conn = sqlite3.connect("pharmasearch.db")

cursor = conn.cursor()

for _, row in df.iterrows():

    cursor.execute("""
        INSERT INTO medicines
        (substance, product, company, country, status, source)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        row["substance"],
        row["product"],
        row["company"],
        row["country"],
        row["status"],
        row["source"]
    ))

conn.commit()

conn.close()

print("Excel data imported successfully")
