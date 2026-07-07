from pandas import DataFrame
from pathlib import Path
import csv
import duckdb
from common.logger import logger


def _csv_source(path: Path) -> tuple[str, str]:
    """
    Returns:
      - DuckDB read_csv_auto source
      - Table alias
    """
    if path.is_dir():
        # Folder → all CSVs
        return f"{path}/*.csv", path.name
    else:
        # Single CSV
        return str(path), path.parent.name

def quote_identifier(field: str) -> str:
    parts = field.split(".")
    if len(parts) == 1:
        return f'"{parts[0]}"'
    return f'{parts[0]}."{parts[1]}"'


def _get_csv_columns(path: Path) -> set[str]:
    """Read the header row of a CSV (or the first CSV in a directory) and return column names."""
    if path.is_dir():
        csv_files = sorted(path.glob("*.csv"))
        if not csv_files:
            return set()
        csv_path = csv_files[0]
    else:
        csv_path = path

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
    return set(header) if header else set()

def fetch_all(
    fields: list[str],
    base_table: Path,
    tables: list[Path] = [],
    joins: list[dict] = [],
    filters: list[dict] = [],
) -> DataFrame:

    # Map folder name → DuckDB table alias
    table_aliases = {table.name: table for table in tables}

    # Driving table alias (folder name or filename)
    base_source, base_alias = _csv_source(base_table)

    # Validate requested fields against actual CSV columns (base + joined tables)
    available_columns_by_table: dict[str, set[str]] = {
        base_alias: _get_csv_columns(base_table),
    }
    for alias, table_path in table_aliases.items():
        available_columns_by_table[alias] = _get_csv_columns(table_path)

    valid_fields = []
    missing_fields = []
    for f in fields:
        # Extract the column name (after the dot for "table.column" format)
        col_name = f.split(".")[-1]
        table_prefix = f.split(".")[0] if "." in f else base_alias
        available_columns = available_columns_by_table.get(table_prefix)

        # If we can resolve the table alias, validate strictly; otherwise keep it
        # to preserve existing behavior for unusual/derived aliases.
        if available_columns is not None:
            if col_name in available_columns:
                valid_fields.append(f)
            else:
                missing_fields.append(f)
        else:
            valid_fields.append(f)

    if missing_fields:
        logger.warning(
            f"Skipping fields not found in CSV sources: {missing_fields}"
        )

    # SELECT clause
    select_clause = ", ".join(quote_identifier(f) for f in valid_fields)
    if not select_clause:
        select_clause = "'' AS _noop"

    sql = f"""
    SELECT DISTINCT {select_clause}
    FROM read_csv_auto('{base_source}') AS {base_alias}
    """

    # JOINs
    if table_aliases and joins:
        for join in joins:
            left_table = join["left_table"]
            left_key = join["left_key"]
            right_table = join.get("right_table")
            right_key = join["right_key"]
            join_type = join.get("type", "left").upper()

            if right_table:
                right_path = table_aliases.get(right_table)
                if not right_path:
                    raise ValueError(f"Join table '{right_table}' not found in tables list")
                
                sql += f"""
                {join_type} JOIN read_csv_auto('{right_path}/*.csv') AS {right_table}
                ON {left_table}.{left_key} = {right_table}.{right_key}
                """

            if "right_derived" in join:
                derived = join["right_derived"]
                right_table = derived["table"]
                right_alias = join.get("right_alias", right_table)
                right_key = join["right_key"]

                right_path = table_aliases.get(right_table)
                if not right_path:
                    raise ValueError(f"Join table '{right_table}' not found in tables list")

                partition_by = derived["partition_by"]
                order_by = derived["order_by"]
                order = derived.get("order", "DESC")

                sql += f"""
                {join_type} JOIN (
                    SELECT *,
                        ROW_NUMBER() OVER (
                            PARTITION BY {partition_by}
                            ORDER BY {order_by} {order}
                        ) AS rn
                    FROM read_csv_auto('{right_path}/*.csv')
                ) AS {right_alias}
                ON {left_table}.{left_key} = {right_alias}.{right_key}
                AND {right_alias}.rn = 1
                """

    # WHERE clause (filters)
    where_clauses = []
    limit_value: int | None = None

    for f in filters:
        table = f["table"]
        field = f["field"]
        op = f["operator"]
        value = f.get("value")

        
        # Special operator: capture record limit
        if op == "count":
            # Expect: {"operator": "count", "count": 2}
            count_raw = f.get("count")
            if count_raw is None:
                raise ValueError("Operator 'count' requires a 'count' key")
            try:
                limit_value = int(count_raw)
                if limit_value <= 0:
                    raise ValueError("Limit must be a positive integer")
            except Exception:
                raise ValueError(f"Invalid count value: {count_raw}")
            continue


        column = f"{table}.{field}"

        if op == "is_null":
            where_clauses.append(f"{column} IS NULL")

        elif op == "is_not_null":
            where_clauses.append(f"{column} IS NOT NULL")

        elif op == "equals":
            where_clauses.append(f"{column} = '{value}'")

        elif op == "not_equals":
            where_clauses.append(f"{column} <> '{value}'")

        elif op == "in":
            values = ", ".join(f"'{v}'" for v in value)
            where_clauses.append(f"{column} IN ({values})")

        elif op == "not_in":
            values = ", ".join(f"'{v}'" for v in value)
            where_clauses.append(f"{column} NOT IN ({values})")

        else:
            raise ValueError(f"Unsupported operator: {op}")

    if where_clauses:
        sql += "\nWHERE " + " AND ".join(where_clauses)

    if limit_value is not None:
        sql += f"\nLIMIT {limit_value}"
    
    conn = duckdb.connect()
    try:
        result = conn.execute(sql).fetchdf()

        # Removes all NaN and convert to string
        str_cols = result.select_dtypes(include=["object", "string"]).columns
        result[str_cols] = result[str_cols].fillna("")

        # Fill NaN in integer columns with 0 before converting to string
        int_cols = result.select_dtypes(include=["Int64", "Int32"]).columns
        result[int_cols] = result[int_cols].fillna(0)

        dt_cols = result.select_dtypes(include=["datetime64[ns]", "datetime64[ns, UTC]"]).columns
        result[dt_cols] = (
            result[dt_cols]
            .apply(lambda s: s.dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            .fillna("")
        )   

        # Add empty-string columns for any fields that were skipped
        for mf in missing_fields:
            col_name = mf.split(".")[-1]
            if col_name not in result.columns:
                result[col_name] = ""

        return result.astype(str)
    except Exception as e:
        raise RuntimeError(f"DuckDB query failed: {e}\nSQL:\n{sql}")
    finally:
        conn.close()


def get_salesforce_object_id(table_path: Path, sys_id: str) -> DataFrame:
    sql = f"""
            SELECT sf__Id
            FROM read_csv_auto('{table_path}/*.*') AS mapping
            WHERE mapping.ServiceNowId__c = '{sys_id}'
            """
    conn = duckdb.connect()
    try:
        result = conn.execute(sql).fetchdf().fillna("")
        if not result.empty:
            return result.iloc[0]["sf__Id"]
    finally:
        conn.close()
