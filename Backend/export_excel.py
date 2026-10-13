import sqlite3
import pandas as pd

conn = sqlite3.connect("pharmasearch.db")

query = "SELECT * FROM medicines"

df = pd.read_sql_query(query, conn)

df.to_excel("exports/medicines_export.xlsx", index=False)

conn.close()

print("Excel exported successfully")
