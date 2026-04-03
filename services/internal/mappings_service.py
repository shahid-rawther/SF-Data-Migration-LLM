import csv
import pathlib
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



def get_mappings(mapping_jsons):
    mappings = []
    for file_name in mapping_jsons:
        if ".json" not in file_name.lower():
            file_name = file_name + ".json"
        mappings.append(read_json_from_file(PATH_TO_MAPPING / file_name))
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
                logger.error(f"No match found for SysId: {row[SERVICE_NOW_ID_COLUMN]}, Hash: {hash_key}")
                continue  # no match found

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
