from pandas import DataFrame
from pathlib import Path
import duckdb


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

    # SELECT clause
    select_clause = ", ".join(quote_identifier(f) for f in fields)

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
