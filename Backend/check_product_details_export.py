import sqlite3
import pandas as pd

conn = sqlite3.connect("pharmasearch.db")

query = """
SELECT
    product,
    substance,
    company,
    status,
    atc_code,
    registration_date,
    product_url
FROM product_details
"""

df = pd.read_sql_query(query, conn)

print(df)

conn.close()
