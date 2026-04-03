from typing import Iterator, Dict, List, Tuple
from pathlib import Path
import csv
import pandas as pd
import io
import sys

from config.settings import (
    HASH_KEY_NAME,
    SALESFORCE_CREATED_AT_COLUMN,
    SALESFORCE_ID_COLUMN,
    SERVICE_NOW_ID_COLUMN,
    MAX_FILE_SIZE
)
from helpers.hash_helper import create_hash_for_row


def save_as_csv(path: Path, data: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Collect all keys
    fieldnames = set()
    for row in data:
        fieldnames.update(row.keys())
    fieldnames = list(fieldnames)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL )
        writer.writeheader()
        writer.writerows(data)


def read_csv_as_dictionary(obj: Path | str) -> Iterator[Dict[str, str]]:
    if isinstance(obj, Path):
        file = open(obj, "r", newline="", encoding="utf-8")
        return csv.DictReader(file)
    if isinstance(obj, str):
        df = pd.read_csv(io.StringIO(obj), dtype=str).fillna("")
        data = df.to_dict(orient="records")
        return data
    raise ValueError(f"Invalid data type")


def convert_to_salesforce_hash(csv: List[Dict]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for row in csv:
        sales_force_id = row.pop(SALESFORCE_ID_COLUMN) if SALESFORCE_ID_COLUMN in row else ""
        _ = row.pop(SALESFORCE_CREATED_AT_COLUMN) if SALESFORCE_CREATED_AT_COLUMN in row else ""
        hash_value = create_hash_for_row(row)
        result.append(
            {
                SALESFORCE_ID_COLUMN: sales_force_id,
                HASH_KEY_NAME: hash_value,
            }
        )
    return result


def convert_to_servicenow_hash(csv: List[Dict]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for row in csv:
        servicenow_id = row[SERVICE_NOW_ID_COLUMN]
        hash_value = create_hash_for_row(row)
        result.append(
            {
                SALESFORCE_ID_COLUMN: servicenow_id,
                HASH_KEY_NAME: hash_value,
            }
        )
    return result


def select_fields_from_csv_text(csv_text: str, fields: List) -> List[Dict[str, str]]:
    df = pd.read_csv(io.StringIO(csv_text), dtype=str).fillna("")

    for field in fields:
        if field not in df.columns:
            raise ValueError(f"Missing required column: {field}")

    result: List[Dict[str, str]] = []

    for _, row in df.iterrows():
        row_dict = row.to_dict()

        sales_force_id = row_dict.pop(SALESFORCE_ID_COLUMN)
        _ = row_dict.pop(SALESFORCE_CREATED_AT_COLUMN)

        hash_value = create_hash_for_row(row_dict)

        result.append(
            {
                SALESFORCE_ID_COLUMN: sales_force_id,
                HASH_KEY_NAME: hash_value,
            }
        )

    return result


def save_df_as_csv(dataframe: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    dataframe.to_csv(path, index=False, lineterminator="\n")



def write_csv_split_by_size(rows:list[dict], output_dir: Path, base_filename: str):
    
    fieldnames = list(rows[0].keys())

    file_index = 1
    current_size = 0
    current_file = None
    writer = None

    def open_new_file():
        nonlocal current_file, writer, current_size, file_index
        if current_file:
            current_file.close()

        file_path = output_dir / f"{base_filename}/{file_index}.csv"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        current_file = open(file_path, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(current_file, fieldnames=fieldnames)
        writer.writeheader()
        current_size = current_file.tell()
        file_index += 1

    open_new_file()

    for row in rows:
        # workaround: use StringIO instead
        from io import StringIO

        buffer = StringIO()
        temp_writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        temp_writer.writerow(row)

        row_bytes = buffer.getvalue().encode("utf-8")
        row_size = len(row_bytes)

        # If adding this row exceeds max size → new file
        if current_size + row_size > MAX_FILE_SIZE:
            open_new_file()

        current_file.write(buffer.getvalue())
        current_size += row_size

    if current_file:
        current_file.close()