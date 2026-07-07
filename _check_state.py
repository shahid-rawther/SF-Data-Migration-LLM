import dotenv; dotenv.load_dotenv()
from services.external.db_client import db_client

# Check ALL job_tables entries
rows = db_client.fetch_all("SELECT * FROM job_tables ORDER BY id DESC LIMIT 10")
print(f"job_tables ({len(rows)} rows):")
for r in rows:
    print(f"  {r}")

print()
rows2 = db_client.fetch_all("SELECT * FROM jobs ORDER BY id")
print(f"jobs ({len(rows2)} rows):")
for r in rows2:
    print(f"  {r}")

print()
rows3 = db_client.fetch_all("SELECT * FROM job_table_batches ORDER BY id DESC LIMIT 10")
print(f"job_table_batches ({len(rows3)} rows):")
for r in rows3:
    print(f"  {r}")
