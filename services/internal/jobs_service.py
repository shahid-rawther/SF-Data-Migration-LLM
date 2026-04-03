import json
from services.external.db_client import db_client, writer

PENDING = "pending"
PROGRESS = "in-progress"
SUCCESS = "success"


def add_job(job_type: str, status: str = PENDING) -> int:
    query = "INSERT INTO jobs (job_type, status )VALUES (?, ?)"
    params = (job_type, status)
    job_id = db_client.insert(query, params)
    return job_id



def get_or_create_job(
    job_id: int,
    job_type: str,
    status: str = PENDING,
) -> int:
    select_query = """
        SELECT id
        FROM jobs
        WHERE id = ?
    """
    row = db_client.fetch_one(select_query, (job_id,))

    if row:
        return row[0]

    insert_query = """
        INSERT INTO jobs (id, job_type, status)
        VALUES (?, ?, ?)
    """
    future =  writer.execute(insert_query, (job_id, job_type, status))
    result = future.result()
    return result["lastrowid"]

def create_job_table(
        job_id: int,
        table_name: str,
        status: str = PENDING,
)->int:
    insert_query = """
        INSERT INTO job_tables (job_id, table_name, status)
        VALUES (?, ?, ?)
    """
    future =  writer.execute(insert_query, (job_id, table_name, status))
    result = future.result()
    return result["lastrowid"]


def get_or_create_job_table(
    job_id: int,
    table_name: str,
    status: str = PENDING,
) -> int:
    select_query = """
        SELECT id
        FROM job_tables
        WHERE job_id = ? AND table_name = ?
    """
    row = db_client.fetch_one(select_query, (job_id, table_name))

    if row:
        return row[0]

    insert_query = """
        INSERT INTO job_tables (job_id, table_name, status)
        VALUES (?, ?, ?)
    """
    future =  writer.execute(insert_query, (job_id, table_name, status))
    result = future.result()
    return result["lastrowid"]


def get_or_create_job_table_batch(
    job_table_id: int,
    batch_size: int,
    batch_num: int,
    batch_operation: str = "insert",
    status: str = PENDING,
    salesforce_job_id: str = "",
) -> int:
    select_query = """
        SELECT id
        FROM job_table_batches
        WHERE job_table_id = ?
          AND batch_number = ?
          AND batch_operation = ?
    """
    row = db_client.fetch_one(select_query, (job_table_id, batch_num, batch_operation))

    if row:
        return row[0]

    insert_query = """
        INSERT INTO job_table_batches
            (job_table_id, batch_size, batch_number, batch_operation, status, salesforce_job_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    return db_client.insert(
        insert_query,
        (job_table_id, batch_size, batch_num, batch_operation, status, salesforce_job_id),
    )


def get_or_create_job_table_sub_batch(
    job_table_batch_id: int,
    batch_size: int,
    batch_num: int,
    status: str = PENDING,
) -> int:
    select_query = """
        SELECT id
        FROM job_table_sub_batches
        WHERE job_table_batch_id = ?
          AND batch_number = ?
    """
    row = db_client.fetch_one(select_query, (job_table_batch_id, batch_num))

    if row:
        return row[0]

    insert_query = """
        INSERT INTO job_table_sub_batches
            (job_table_batch_id, batch_size, batch_number, status)
        VALUES (?, ?, ?, ?)
    """
    return db_client.insert(
        insert_query,
        (job_table_batch_id, batch_size, batch_num, status),
    )

def is_job_table_success(job_table_id: int) -> bool:
    query = "SELECT status FROM job_tables WHERE id = ?"
    row = db_client.fetch_one(query, (job_table_id,))
    return bool(row and row[0] == SUCCESS)


def is_job_table_batch_success(job_table_batch_id: int) -> bool:
    query = "SELECT status FROM job_table_batches WHERE id = ?"
    row = db_client.fetch_one(query, (job_table_batch_id,))
    return bool(row and row[0] == SUCCESS)

def is_job_table_sub_batch_success(job_table_sub_batch_id: int) -> bool:
    query = "SELECT status FROM job_table_sub_batches WHERE id = ?"
    row = db_client.fetch_one(query, (job_table_sub_batch_id,))
    return bool(row and row[0] == SUCCESS)


def update_job_status(job_id: int, status: str) -> None:
    query = "UPDATE jobs SET status = ? WHERE id = ?"
    params = (status, job_id)
    writer.execute(query, params)


def update_job_table_status(job_table_id: int, status: str) -> None:
    query = "UPDATE job_tables SET status = ? WHERE id = ?"
    params = (status, job_table_id)
    writer.execute(query, params)


def update_job_table_batch_status(job_table_batch_id: int, status: str) -> None:
    query = "UPDATE job_table_batches SET status = ? WHERE id = ?"
    params = (status, job_table_batch_id)
    writer.execute(query, params)

def update_job_table_sub_batch_status(job_table_sub_batch_id: int, status: str) -> None:
    query = "UPDATE job_table_sub_batches SET status = ? WHERE id = ?"
    params = (status, job_table_sub_batch_id)
    writer.execute(query, params)

def update_job_table_batch_salesforce_job_id(job_table_batch_id: int, salesforce_job_id: str) -> None:
    query = "UPDATE job_table_batches SET salesforce_job_id = ? WHERE id = ?"
    params = (salesforce_job_id, job_table_batch_id)
    writer.execute(query, params)


def update_job_table_batch_results(job_table_batch_id: int, result: dict) -> None:
    result_str = json.dumps(result)
    query = "UPDATE job_table_batches SET results = ? WHERE id = ?"
    params = (result_str, job_table_batch_id)
    writer.execute(query, params)
