import csv
import json
import pathlib
from datetime import datetime, timezone
from typing import Dict, List
from tools.file_manager import read_json_from_file
from config.settings import (
    HASH_KEY_NAME,
    PATH_TO_MAPPING,
    SALESFORCE_ID_COLUMN,
    SERVICE_NOW_ID_COLUMN,
)
from services.external.db_client import db_client
from common.logger import logger
from tools.cache import execution_cache

_PRODUCT_ID_MAPPING_TABLE = "sc_cat_item_Product2_Product"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cfg_source_tables(config: dict) -> list[str]:
    """Read source tables from a mapping config, supporting both generic and legacy keys."""
    return config.get("source_tables") or config.get("servicenow_tables", [])


def cfg_destination_object(config: dict) -> str:
    """Read destination object from a mapping config, supporting both generic and legacy keys."""
    return config.get("destination_object") or config.get("salesforce_object", "")


def cfg_external_id_field(config: dict) -> str:
    """Return the Salesforce external-id field configured for upsert-based dedup.

    When set, inserts are performed as Bulk API ``upsert`` keyed on this field so
    that re-running a migration (even after the local id_mappings DB is reset)
    updates the existing Salesforce record instead of creating a duplicate.
    Empty string means no external-id strategy is configured (default behaviour).
    """
    return config.get("external_id_field") or config.get("external_id") or ""


def _load_mapping_from_json(name: str) -> dict | None:
    """Load a mapping config from the on-disk JSON file (fallback path).

    Searches the ServiceNow mappings directory first, then the JSM directory.
    """
    from config.settings import PATH_TO_JSM_MAPPING

    file_name = name if name.lower().endswith(".json") else f"{name}.json"
    for mapping_dir in (PATH_TO_MAPPING, PATH_TO_JSM_MAPPING):
        path = mapping_dir / file_name
        if path.exists():
            try:
                return read_json_from_file(path)
            except Exception:
                continue
    return None


def get_mapping_config(name: str) -> dict | None:
    """Return the mapping config blob for a single object.

    Reads from the mapping_configs DB table first; falls back to the shipped
    JSON file when the DB has no entry (keeps backward compatibility).
    The ``source_type`` stored in the DB column is always injected into the
    returned dict so callers can rely on it regardless of whether the JSON
    blob contained it.
    """
    if name.lower().endswith(".json"):
        name = name[: -len(".json")]
    try:
        row = db_client.fetch_one(
            "SELECT config, source_type FROM mapping_configs WHERE name = ?",
            (name,),
        )
    except Exception:
        row = None
    if row and row[0]:
        try:
            config = json.loads(row[0])
            if row[1]:
                config.setdefault("source_type", row[1])
            return config
        except Exception:
            pass
    return _load_mapping_from_json(name)


def list_mapping_configs(source_type: str | None = None) -> list[dict]:
    """Return metadata for all mappings (without the heavy config blob)."""
    if source_type:
        rows = db_client.fetch_all(
            "SELECT name, source_type, priority, is_system, created_at, updated_at "
            "FROM mapping_configs WHERE source_type = ? ORDER BY priority, name",
            (source_type,),
        )
    else:
        rows = db_client.fetch_all(
            "SELECT name, source_type, priority, is_system, created_at, updated_at "
            "FROM mapping_configs ORDER BY priority, name"
        )
    keys = ["name", "source_type", "priority", "is_system", "created_at", "updated_at"]
    result = []
    for r in rows:
        d = dict(zip(keys, r))
        d["is_system"] = bool(d["is_system"])
        result.append(d)
    return result


def summarise_mappings(source_type: str | None = None) -> list[dict]:
    """Return a light summary of every mapping: what it reads and what it writes.

    Each entry carries ``destination_object``, ``source_tables`` and the
    destination fields the mapping actually populates. Callers use the empty
    destination object to tell a real migration target from an extract-only
    config, which reads a hundred tables and writes to nothing.
    """
    try:
        if source_type:
            rows = db_client.fetch_all(
                "SELECT name, config FROM mapping_configs WHERE source_type = ? ORDER BY priority, name",
                (source_type,),
            )
        else:
            rows = db_client.fetch_all(
                "SELECT name, config FROM mapping_configs ORDER BY priority, name"
            )
    except Exception:
        rows = []

    summaries: list[dict] = []
    for name, raw_config in rows:
        try:
            config = json.loads(raw_config or "{}")
        except Exception:
            continue
        summaries.append(
            {
                "name": name,
                "destination_object": cfg_destination_object(config),
                "source_tables": [t for t in cfg_source_tables(config) if t],
                "mapped_destination_fields": mapped_destination_fields(config),
            }
        )
    return summaries


def index_by_destination_object(source_type: str | None = None) -> dict[str, list[dict]]:
    """Return Salesforce object -> the mappings that populate it.

    Answers "will anything migrate this object?", which is what turns a bare
    prerequisite into an actionable one: an object with a mapping can simply be
    added to the run, an object without one has to be built first.
    """
    index: dict[str, list[dict]] = {}
    for entry in summarise_mappings(source_type):
        destination = entry["destination_object"]
        if destination:
            index.setdefault(destination, []).append(entry)
    return index


def mapped_destination_fields(config: dict) -> set[str]:
    """Return the destination fields a config actually populates."""
    populated: set[str] = set()
    for entry in config.get("fields", []) or []:
        destination = entry.get("destination_field", "")
        has_value = (
            bool(entry.get("source_field"))
            or entry.get("constant_value") is not None
            or bool(entry.get("use_constant"))
        )
        if destination and has_value:
            populated.add(destination)
    return populated


def save_mapping_config(
    name: str,
    config: dict,
    source_type: str = "servicenow",
    priority: int | None = None,
) -> dict:
    """Create or update a mapping config.

    New mappings are stored with is_system=0 (user-created). Updating an
    existing mapping preserves its current is_system flag so that shipped
    system mappings remain protected even after being edited.
    """
    if priority is None:
        priority = config.get("priority", 9999)
    existing = db_client.fetch_one(
        "SELECT is_system FROM mapping_configs WHERE name = ?",
        (name,),
    )
    is_system = existing[0] if existing else 0
    now = _now()
    db_client.bulk_upsert(
        "mapping_configs",
        [
            {
                "name": name,
                "source_type": source_type,
                "config": json.dumps(config),
                "priority": priority,
                "is_system": is_system,
                "updated_at": now,
            }
        ],
        ["name"],
        ignore_cols={"id", "created_at"},
    )
    return {
        "name": name,
        "source_type": source_type,
        "priority": priority,
        "is_system": bool(is_system),
    }


def import_json_mappings(overwrite: bool = False) -> dict:
    """Import the shipped JSON mapping files into the mapping_configs table.

    Used as a one-time/refresh seed. Imported mappings are flagged is_system=1.
    When overwrite is False, existing names are left untouched.
    Scans both ServiceNow and JSM mapping directories.
    Returns a summary of imported and skipped names.
    """
    from config.settings import (
        MAPPING_PRIORITY, ATTACHMENT_MAPPING_PRIORITY,
        JSM_MAPPING_PRIORITY, PATH_TO_JSM_MAPPING,
    )

    priority_map: dict[str, int] = {}
    for idx, group in enumerate(MAPPING_PRIORITY + ATTACHMENT_MAPPING_PRIORITY):
        for nm in group:
            priority_map.setdefault(nm, idx)
    for idx, group in enumerate(JSM_MAPPING_PRIORITY):
        for nm in group:
            priority_map.setdefault(nm, idx)

    existing = {m["name"] for m in list_mapping_configs()}
    imported: list[str] = []
    skipped: list[str] = []
    now = _now()
    rows = []

    mapping_dirs: list[tuple[pathlib.Path, str]] = [
        (PATH_TO_MAPPING, "servicenow"),
        (PATH_TO_JSM_MAPPING, "jsm"),
    ]

    for mapping_dir, default_source_type in mapping_dirs:
        if not mapping_dir.exists():
            continue
        for json_file in sorted(mapping_dir.glob("*.json")):
            name = json_file.stem
            if name in existing and not overwrite:
                skipped.append(name)
                continue
            try:
                config = read_json_from_file(json_file)
            except Exception:
                skipped.append(name)
                continue
            source_type = config.get("source_type", default_source_type)
            priority = priority_map.get(name, config.get("priority", 9999))
            rows.append({
                "name": name,
                "source_type": source_type,
                "config": json.dumps(config),
                "priority": priority,
                "is_system": 1,
                "updated_at": now,
            })
            imported.append(name)
    if rows:
        db_client.bulk_upsert("mapping_configs", rows, ["name"], ignore_cols={"id", "created_at"})
    return {"imported": imported, "skipped": skipped}


def delete_mapping_config(name: str) -> bool:
    """Delete a user-created mapping. System mappings cannot be deleted."""
    row = db_client.fetch_one(
        "SELECT is_system FROM mapping_configs WHERE name = ?",
        (name,),
    )
    if not row:
        return False
    if row[0]:
        raise ValueError("System mappings cannot be deleted")
    db_client.execute("DELETE FROM mapping_configs WHERE name = ?", (name,))
    return True


def get_priority_groups(source_type: str | None = None) -> list[list[str]]:
    """Return ordered priority groups derived from the mapping_configs table.

    Mappings sharing the same priority value are grouped together (preserving
    the original parallel-group structure, since seeded priorities equal the
    group index in MAPPING_PRIORITY). Groups are ordered ascending by priority.
    Falls back to the hardcoded settings groups when the DB is empty (e.g.
    before seeding has run).
    """
    try:
        rows = (
            db_client.fetch_all(
                "SELECT name, priority FROM mapping_configs WHERE source_type = ? ORDER BY priority, name",
                (source_type,),
            )
            if source_type
            else db_client.fetch_all(
                "SELECT name, priority FROM mapping_configs ORDER BY priority, name"
            )
        )
    except Exception:
        rows = []
    if rows:
        grouped: dict[int, list[str]] = {}
        for name, priority in rows:
            grouped.setdefault(priority, []).append(name)
        return [grouped[p] for p in sorted(grouped)]
    from config.settings import MAPPING_PRIORITY, ATTACHMENT_MAPPING_PRIORITY
    return MAPPING_PRIORITY + ATTACHMENT_MAPPING_PRIORITY


def get_mappings(mapping_jsons):
    """Load full mapping configs for the given object names.

    Reads from the mapping_configs DB table first, falling back to the JSON
    files on disk for any name not present in the DB. Returned list is sorted
    by each mapping's "priority" key (ascending) to preserve existing behavior.
    """
    mappings = []
    for file_name in mapping_jsons:
        name = file_name[: -len(".json")] if file_name.lower().endswith(".json") else file_name
        config = get_mapping_config(name)
        if config is not None:
            mappings.append(config)
    mappings = sorted(mappings, key=lambda o: o.get("priority", 9999))
    return mappings

def is_exists(table_names: str, sys_id: str):
    def _load_all_id_mappings():
        result = get_sales_force_id_mappings_for_tables(
            table_names
        )
        return {row[1]: row[0] for row in result}
    cache_key = f"ID_MAPPING::{"#".join(table_names)}"
    mappings: dict[str, str] = execution_cache.get_or_set(
        cache_key, lambda: _load_all_id_mappings()
    )
    if mappings.get(sys_id):
        return True


def map_servicenow_to_salesforce(
    servicenow_hash_path: pathlib.Path,
    salesforce_hash: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """
    Returns mapping of ServiceNow sys_id to Salesforce Id using hash_key
    """
    logger.info(servicenow_hash_path)
    # Build lookup: hash_key -> salesforce_id
    sf_hash_lookup = {
        row[HASH_KEY_NAME]: row[SALESFORCE_ID_COLUMN]
        for row in salesforce_hash
        if row.get(HASH_KEY_NAME)
    }

    mappings: List[Dict[str, str]] = []

    with open(servicenow_hash_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            hash_key = row.get(HASH_KEY_NAME)

            if not hash_key:
                continue

            salesforce_id = sf_hash_lookup.get(hash_key)
            if not salesforce_id:
                # Expected, and not yet a failure. This pass matches on the hash
                # computed at transform time, which no longer describes rows the
                # loader altered before upload, so the caller runs a
                # recomputed-hash fallback over exactly these rows. Logged at
                # error level this produced one line per row for a condition that
                # normally self-heals — the caller reports the aggregate, and
                # only raises an error for a shortfall that survives the
                # fallback.
                logger.debug(
                    "First-pass hash miss for SysId %s (hash %s); left to the fallback",
                    row[SERVICE_NOW_ID_COLUMN],
                    hash_key,
                )
                continue

            mappings.append(
                {
                    SERVICE_NOW_ID_COLUMN: row[SERVICE_NOW_ID_COLUMN],
                    SALESFORCE_ID_COLUMN: salesforce_id,
                    HASH_KEY_NAME: hash_key,
                }
            )

    return mappings


def validate_mappings(table_mappings: list[dict]):
    return True


def get_id_mapping_table_name(
    service_now_driving_table, sales_force_object_name, trasformation_name=None
):
    return (
        service_now_driving_table
        + "_"
        + sales_force_object_name
        + "_"
        + (trasformation_name if trasformation_name else "")
    )

def delete_id_mappings(table_name, servicenow_ids: list[str]):
    placeholders = ",".join(["?"] * len(servicenow_ids))
    sql = f"""
        DELETE FROM id_mappings
        WHERE table_name = ?
        AND servicenow_id IN ({placeholders})
    """

    params = [table_name, *servicenow_ids]
    db_client.execute(sql, params)

def delete_id_mappings_by_salesforce_id(table_name, salesforce_ids: list[str]):
    placeholders = ",".join(["?"] * len(salesforce_ids))
    sql = f"""
        DELETE FROM id_mappings
        WHERE table_name = ?
        AND salesforce_id IN ({placeholders})
    """

    params = [table_name, *salesforce_ids]
    db_client.execute(sql, params)

def insert_id_mappings(table_name: str, id_mappings: list[dict]):
    logger.info("Inserting ID mappings... for table: %s", table_name)
    mappings = [
        {
            "table_name": table_name,
            "servicenow_id": mappings[SERVICE_NOW_ID_COLUMN],
            "salesforce_id": mappings[SALESFORCE_ID_COLUMN],
            "hash_key": mappings.get(HASH_KEY_NAME) if mappings.get(HASH_KEY_NAME) else "",
        }
        for mappings in id_mappings
    ]
    db_client.bulk_upsert("id_mappings", mappings, ["table_name", "servicenow_id"])
    logger.info(f"Inserting ID mappings... for table: {table_name} Done")

def get_sales_force_id(service_now_id: str, table_names: list[str]) -> str | None:
    placeholders = ",".join("?" for _ in table_names)

    select_query = f"""
        SELECT salesforce_id
        FROM id_mappings
        WHERE servicenow_id = ?
          AND table_name IN ({placeholders})
        LIMIT 1
    """

    params = (service_now_id, *table_names)
    row = db_client.fetch_one(select_query, params)

    return row[0] if row else None


def get_sales_force_id_by_sys_id(service_now_id: str):

    select_query = """
        SELECT salesforce_id
        FROM id_mappings
        WHERE servicenow_id = ?
    """
    row = db_client.fetch_one(select_query, (service_now_id,))
    if row:
        return row[0]


def get_sales_force_id_by_sys_id_and_table_name(service_now_id: str, table_name: str):

    select_query = """
        SELECT salesforce_id
        FROM id_mappings
        WHERE servicenow_id = ? AND table_name = ?
    """
    row = db_client.fetch_one(select_query, (service_now_id, table_name))
    if row:
        return row[0]

def get_sales_force_id_mappings_for_tables(table_names: list[str]):
    placeholders = ",".join("?" for _ in table_names)

    select_query = f"""
        SELECT salesforce_id, servicenow_id
        FROM id_mappings
        WHERE  table_name IN ({placeholders})
    """
    params = (*table_names,)
    rows = db_client.fetch_all(select_query, params)
    return rows

def get_all_id_mapping_table_name():

    select_query = f"""
        SELECT table_name
        FROM id_mappings
        GROUP BY table_name
    """
    params = ()
    rows = db_client.fetch_all(select_query, params)
    return rows

def get_sales_force_id_by_sys_id_and_table_names(
    service_now_id: str,
    table_names: list[str],
):
    if not table_names:
        return None

    placeholders = ",".join("?" for _ in table_names)

    select_query = f"""
        SELECT salesforce_id
        FROM id_mappings
        WHERE servicenow_id = ?
          AND table_name IN ({placeholders})
        LIMIT 1
    """

    params = (service_now_id, *table_names)
    row = db_client.fetch_one(select_query, params)

    return row[0] if row else None



def get_id_mappings():
    select_query = f"""
        SELECT servicenow_id, salesforce_id
        FROM id_mappings
    """
    return db_client.fetch_all(select_query)


def count_migrated_for_source_table(source_table: str) -> int:
    """Count distinct source IDs already migrated for a given source table.

    The ``id_mappings.table_name`` column follows the format
    ``{source_table}_{sf_object}_{mapping_name}``.  We match rows whose
    table_name starts with ``{source_table}_`` so that all mapping objects
    sharing this source table are counted.
    """
    row = db_client.fetch_one(
        "SELECT COUNT(DISTINCT servicenow_id) FROM id_mappings WHERE table_name LIKE ?",
        (f"{source_table}_%",),
    )
    return row[0] if row else 0


def count_id_mappings(table_name: str) -> int:
    """Count the ids held under one exact ``id_mappings`` table name.

    Unlike :func:`count_migrated_for_source_table` this does not match a prefix,
    because a lookup reads one specific table and a sibling mapping's ids are of
    no use to it.
    """
    if not table_name:
        return 0
    row = db_client.fetch_one(
        "SELECT COUNT(*) FROM id_mappings WHERE table_name = ?",
        (table_name,),
    )
    return row[0] if row else 0


def lookup_targets_for_object(
    destination_object: str,
    source_type: str | None = None,
) -> list[dict]:
    """Where a lookup to ``destination_object`` can get its destination ids.

    A lookup field does not point at an object, it reads an ``id_mappings``
    table, and that table's name is built from the *parent* mapping's driving
    table, destination object and transformation name. A child mapping knows none
    of those, so the name has to be resolved from the catalog rather than guessed
    from the object name — guessing produces a name nothing ever writes.

    Ordered most-populated first, so the caller can distinguish "resolves now"
    from "resolves once that mapping runs" by reading the head of the list.
    """
    from agents import wiring

    target = (destination_object or "").strip()
    if not target:
        return []

    targets: list[dict] = []
    for entry in index_by_destination_object(source_type=source_type).get(target, []):
        config = get_mapping_config(entry["name"])
        if not config:
            continue
        table = wiring.lookup_table_name(config)
        if not table:
            continue
        targets.append(
            {
                "mapping_name": entry["name"],
                "source_table": (entry["source_tables"] or [""])[0],
                "lookup_table": table,
                "migrated_count": count_id_mappings(table),
            }
        )

    targets.sort(key=lambda c: (-c["migrated_count"], c["mapping_name"]))
    return targets


def destinations_for_source_table(
    source_table: str,
    source_type: str | None = None,
) -> list[dict]:
    """What a source table is migrated into, and how many of its ids are on hand.

    The mirror of :func:`lookup_targets_for_object`: that one starts from the
    destination object a field points at, this one from the source table a value
    points at. Needed when the destination gives no clue — a text column names no
    object, so the only way to say what its ids could resolve to is to ask what
    the source table they came from becomes.
    """
    from agents import wiring

    table = (source_table or "").strip()
    if not table:
        return []

    results: list[dict] = []
    for entry in summarise_mappings(source_type):
        if table not in (entry["source_tables"] or []):
            continue
        if not entry["destination_object"]:
            continue
        config = get_mapping_config(entry["name"])
        if not config:
            continue
        lookup_table = wiring.lookup_table_name(config)
        results.append(
            {
                "mapping_name": entry["name"],
                "destination_object": entry["destination_object"],
                "lookup_table": lookup_table,
                "migrated_count": count_id_mappings(lookup_table),
            }
        )

    results.sort(key=lambda r: (-r["migrated_count"], r["mapping_name"]))
    return results


def sync_product_id_mappings(
    sn_base: str,
    sn_user: str,
    sn_pass: str,
    sf_credentials: dict,
) -> dict:
    """Fetch catalog items from ServiceNow and products from Salesforce, match
    by name, and persist the pairs into id_mappings.

    Returns a dict with keys: matched (int), unmatched_sn (list), unmatched_sf (list).
    Raises on authentication or network failure so the caller can decide whether
    to propagate or suppress the error.
    """
    import requests

    sn_base = (sn_base or "").rstrip("/")
    if not (sn_base and sn_user and sn_pass):
        raise ValueError("Incomplete ServiceNow credentials (base_url, username, password required)")

    # ── 1. Fetch catalog items from ServiceNow ────────────────────────────────
    sn_items: dict[str, str] = {}  # name.lower() → sys_id
    offset = 0
    page_size = 1000
    while True:
        resp = requests.get(
            f"{sn_base}/api/now/table/sc_cat_item",
            params={
                "sysparm_fields": "sys_id,name",
                "sysparm_limit": page_size,
                "sysparm_offset": offset,
                "sysparm_exclude_reference_link": "true",
            },
            auth=(sn_user, sn_pass),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        records = resp.json().get("result", [])
        for rec in records:
            name = (rec.get("name") or "").strip()
            sys_id = (rec.get("sys_id") or "").strip()
            if name and sys_id:
                sn_items[name.lower()] = sys_id
        if len(records) < page_size:
            break
        offset += page_size

    logger.info("sync_product_id_mappings: fetched %d catalog items from ServiceNow", len(sn_items))

    # ── 2. Authenticate with Salesforce ───────────────────────────────────────
    sf_base = (sf_credentials.get("base_url") or "").rstrip("/")
    client_id = sf_credentials.get("client_id") or ""
    client_secret = sf_credentials.get("client_secret") or ""
    api_version = sf_credentials.get("api_version") or "v60.0"

    token_resp = requests.post(
        f"{sf_base}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    token_resp.raise_for_status()
    token_data = token_resp.json()
    access_token = token_data["access_token"]
    instance_url = (token_data.get("instance_url") or sf_base).rstrip("/")

    # ── 3. Fetch Product2 from Salesforce via SOQL ────────────────────────────
    sf_products: dict[str, str] = {}  # name.lower() → Id
    sf_headers = {"Authorization": f"Bearer {access_token}"}
    next_url: str | None = (
        f"{instance_url}/services/data/{api_version}/query"
        "?q=SELECT+Id,Name+FROM+Product2+ORDER+BY+Name"
    )
    while next_url:
        qresp = requests.get(next_url, headers=sf_headers, timeout=30)
        qresp.raise_for_status()
        data = qresp.json()
        for rec in data.get("records", []):
            name = (rec.get("Name") or "").strip()
            sf_id = (rec.get("Id") or "").strip()
            if name and sf_id:
                sf_products[name.lower()] = sf_id
        next_records = data.get("nextRecordsUrl")
        next_url = f"{instance_url}{next_records}" if next_records else None

    logger.info("sync_product_id_mappings: fetched %d products from Salesforce", len(sf_products))

    # ── 4. Match by name ──────────────────────────────────────────────────────
    matched_mappings: list[dict] = []
    unmatched_sn: list[str] = []

    for name_lower, sys_id in sn_items.items():
        sf_id = sf_products.get(name_lower)
        if sf_id:
            matched_mappings.append({
                SERVICE_NOW_ID_COLUMN: sys_id,
                SALESFORCE_ID_COLUMN: sf_id,
            })
        else:
            unmatched_sn.append(name_lower)

    matched_sf_names = {n for n in sn_items if n in sf_products}
    unmatched_sf = [n for n in sf_products if n not in matched_sf_names]

    # ── 5. Persist and invalidate cache ──────────────────────────────────────
    if matched_mappings:
        insert_id_mappings(_PRODUCT_ID_MAPPING_TABLE, matched_mappings)

    execution_cache.invalidate(f"ID_MAPPING::{_PRODUCT_ID_MAPPING_TABLE}")

    logger.info(
        "sync_product_id_mappings: %d matched, %d unmatched SN items, %d unmatched SF products",
        len(matched_mappings), len(unmatched_sn), len(unmatched_sf),
    )

    return {
        "matched": len(matched_mappings),
        "unmatched_sn": unmatched_sn,
        "unmatched_sf": unmatched_sf,
    }
