"""Check what mapping config the DB has vs the JSON file."""
import dotenv
dotenv.load_dotenv()

import json
from services.external.db_client import db_client

row = db_client.fetch_one(
    "SELECT name, config, source_type FROM mapping_configs WHERE name = ?", ("JSMUser",)
)
if row:
    print(f"=== DB mapping_configs entry for 'JSMUser' ===")
    print(f"source_type column: {row[2]}")
    config = json.loads(row[1])
    print(json.dumps(config, indent=2))
else:
    print("JSMUser NOT found in mapping_configs DB — will use JSON file on disk")
