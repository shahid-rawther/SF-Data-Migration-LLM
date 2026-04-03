import csv
from pathlib import Path
from config.settings import PATH_TO_DATA, PATH_TO_DOWNLOADS, PATH_TO_RESULTS, PATH_TO_TRANSFORMATION, SALESFORCE_ID_COLUMN, SERVICE_NOW_ID_COLUMN
from helpers.transformation_helper import look_up_incident_sys_id
from services.http_clients.servicenow_http_client import servicenow_client
import pandas as pd
import services.external.duckdb_client as duckdb_client
from services.external.db_client import db_client
from tools.csv_manager import read_csv_as_dictionary
from tools.file_manager import save_data_to_file, save_stream_to_file
import duckdb
import shutil

import random
import time
from pathlib import Path


import tqdm
from common.logger import logger
from services.internal.mappings_service import (
    delete_id_mappings,
    get_mappings,
    insert_id_mappings,
    map_servicenow_to_salesforce,
    get_id_mapping_table_name,
)
import services.internal.jobs_service as jobs_service
from services.http_clients.salesforce_http_client import salesforce_client
from config.settings import (
    DISABLE_LOAD,
    MAX_WORKERS,
    PATH_TO_TRANSFORMATION,
    PATH_TO_RESULTS,
    POLL_TIME,
)
from tools.file_manager import save_text_to_file, read_text_from_file
from tools.csv_manager import (
    convert_to_salesforce_hash,
    convert_to_servicenow_hash,
    read_csv_as_dictionary,
    save_as_csv,
)
from concurrent.futures import ThreadPoolExecutor, as_completed
from services.internal import loading_service, transformation_service


def get_total_count(priorities: list[list[dict]]):
    report = []
    for priority in priorities:
        mappings = get_mappings(priority)
        for table_mappings in mappings:
            
            service_now_tables_path = PATH_TO_DOWNLOADS / "servicenow"
            servicenow_tables = table_mappings["servicenow_tables"]
            salesforce_object_name = table_mappings["salesforce_object"]
            driving_table = servicenow_tables[0]
            fields = table_mappings["fields"]
            joins = table_mappings["joins"]
            filters = table_mappings["filters"]
            operation = table_mappings["operation"]
            look_up_table_names = table_mappings.get(
                "look_up_table_names", None
            )
            transformation_name = table_mappings.get("transformation_name")
            id_mapping_table_names = look_up_table_names if look_up_table_names else [
                get_id_mapping_table_name(
                    driving_table, salesforce_object_name, transformation_name
                )
            ]

            id_mapping_table_name = get_id_mapping_table_name(
                    driving_table, salesforce_object_name, transformation_name
                )

            join_tables = []
            for join_table in servicenow_tables[1:]:
                join_tables.append(service_now_tables_path / join_table)
            base_table = service_now_tables_path / table_mappings["servicenow_tables"][0]
            servicenow_source_fields = transformation_service.extract_unique_source_fields(fields)
            servicenow_source_fields.append(f"{driving_table}.{SERVICE_NOW_ID_COLUMN}")
            service_now_source_data = duckdb_client.fetch_all(
                servicenow_source_fields, base_table, join_tables, joins, filters
            )

            # Errors
            # sql = "select id from job_tables where table_name=? and job_id=3 order by id desc"
            # result = db_client.fetch_one(sql, (id_mapping_table_name,))
            # job_table_id = result[0] if result else "invalid"

            # latest_result_path = PATH_TO_RESULTS / (transformation_name if transformation_name else salesforce_object_name)
            # latest_result_path = Path(latest_result_path / f"{job_table_id}")

            # errors = []
            # exclude = [
            #     "FIELD_INTEGRITY_EXCEPTION",
            #     "CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY",
            # ]
            # error_count = 0
            # if latest_result_path.exists():
            #     for batch_path in latest_result_path.iterdir():
            #         added_error_types = []
            #         error_file = batch_path / "failed.csv"
            #         error_batch = max(
            #             batch_path.glob("*_insert"),
            #             key=lambda p: p.stat().st_mtime,
            #             default=None
            #         )
            #         if not error_batch:
            #             continue
            #         error_file = error_batch / "failed.csv"
            #         fails = list(read_csv_as_dictionary(error_file))
            #         error_count += len(fails)
            #         for failed in fails:
            #             error = failed['sf__Error']
            #             splitted_error = error.split(":")
            #             if splitted_error[0] not in added_error_types:
            #                 added_error_types.append(splitted_error[0])
            #                 if "STRING_TOO_LONG" not in splitted_error[0]:
            #                     errors.append(error)
                                
                            

            sql = f"select count(), table_name from id_mappings group by table_name having table_name = ?"
            result = db_client.fetch_one(sql, (id_mapping_table_name,))

            report_for_table = {
                "table_name":(transformation_name if transformation_name else salesforce_object_name) ,
                "total":len(service_now_source_data),
                "migrated": result[0],
                # "failed": error_count
            }
            report.append(report_for_table)
    report_path = PATH_TO_DATA /"report/report.csv"
    save_as_csv(report_path, report)


def list_errors():
    file = PATH_TO_RESULTS
    for migration_results in file.iterdir():
        
        errors = []
        for job_table_dir in migration_results.iterdir():
            if job_table_dir.is_dir():
                error_summary = job_table_dir / "error_summary.txt"
                for batch_dir in job_table_dir.iterdir():
                    if batch_dir.is_dir():
                        for sub_batch_dir in batch_dir.iterdir():
                            failed_records = sub_batch_dir / "failed.csv"
                            if failed_records.is_file() and failed_records.exists():
                                records = read_csv_as_dictionary(failed_records)
                                for record in records:
                                    error = record["sf__Error"] + "\n"
                                    if error not in errors:
                                        errors.append(error)

                with open(error_summary, "w", encoding="utf-8") as error_summary_file:
                    error_summary_file.writelines(errors)


def transformed_file_test_all_csv():
    parent_dir = Path(
        r"C:/Users/pbi2user/Desktop/coolsys-itsm-data-migration/data/results/ParentIncident"
    )
    base = parent_dir.as_posix()
    sf_id = "0nyWC0000001UgkYAE"
    column_name = "Urgency"
    sql = f"""
        SELECT DISTINCT t."{column_name}"
        FROM read_csv_auto('{base}/5000_*/*_insert/success.csv') AS t
        WHERE t.sf__Id = ?
    """

    # Use parameter binding for the value; keep path literal in SQL
    result = duckdb.execute(sql, [sf_id]).fetchdf()
    print(result)


def test_download_results():

    job_ids = [
        # "750Vt00000z0GHnIAM",
        # "750Vt00000z0Dv1IAE",
        "750Vt00000yzqFPIAY"
    ]
    terminal_states = {"JobComplete", "Failed", "Aborted", "Open"}
    job_satus = None
    for job_id in job_ids:
        while True:
            job_satus = salesforce_client.get_job(job_id)
            state = job_satus.get("state")
            if state == "Failed":
                logger.info(job_satus)

            if state in terminal_states:
                break
            time.sleep(POLL_TIME)  # backoff as needed

        success_path = PATH_TO_RESULTS / f"all_results/{job_id}/success.csv"
        failed_path = PATH_TO_RESULTS / f"all_results/{job_id}/failed.csv"
        unprocessed_path = PATH_TO_RESULTS / f"all_results/{job_id}/unprocessed.csv"

        success = salesforce_client.get_results(job_id, "successfulResults")
        save_text_to_file(success, success_path)

        # success_data = read_csv_as_dictionary(success)

        # salesforce_hash = convert_to_salesforce_hash(success_data)
        # servicenow_hash_path = ""
        # servicenow_hash_path = (
        #     PATH_TO_TRANSFORMATION
        #     / "hash"
        #     / (transformation_name if transformation_name else salesforce_object)
        #     / batch_path.parent.stem
        #     / f"{batch_path.stem}_hash.csv"
        # )

        #     id_mappings = map_servicenow_to_salesforce(
        #         servicenow_hash_path, salesforce_hash
        #     )

        #     insert_id_mappings(id_mapping_table_name, id_mappings)

        failed = salesforce_client.get_results(job_id, "failedResults")
        save_text_to_file(failed, failed_path)

        unprocessed = salesforce_client.get_results(job_id, "unprocessedRecords")
        save_text_to_file(unprocessed, unprocessed_path)



def process_all_results(priorities: list[list[str]]):
    for priority in priorities:
        process_mapping(priority)

def process_mapping(table_mappings_names:list[str]):
    table_mappings = get_mappings(table_mappings_names)
    for table_mapping in table_mappings:
        re_arrage_result(table_mapping)



def re_arrage_result(table_mappings:dict):
    

    salesforce_object = table_mappings["salesforce_object"]
    transformation_name = table_mappings.get("transformation_name")
    table_name = table_mappings["servicenow_tables"][0]  # Driving table
    job_table = transformation_service.get_id_mapping_table_name(
        table_name,
        table_mappings["salesforce_object"],
        table_mappings.get("transformation_name"),
    )

    transformation_path = (
        PATH_TO_TRANSFORMATION
        / "csv"
        / (transformation_name if transformation_name else salesforce_object)
    )
    results_path = (
        PATH_TO_RESULTS
        / (transformation_name if transformation_name else salesforce_object)
    )
    source_path = (
        PATH_TO_RESULTS / "all_results"
    )
    if transformation_path.exists():
        for batch_path in transformation_path.iterdir():
            for sub_batch_path in batch_path.iterdir():
                sub_batch_name = sub_batch_path.stem
                batch_number = batch_path.stem.split("_")[1]

                query = """
                    select * from job_table_batches 
                    where job_table_id  in (select id from job_tables where table_name=? and job_id = 3) AND
                    batch_number = ?
                """
        
                result = db_client.fetch_one(query, (job_table,batch_number))
                
                result_folder_name = result[-1]

                target_success_path = results_path / f"{result[1]}/5000_{batch_number}/{sub_batch_name}/success.csv"
                target_failed_path = results_path / f"{result[1]}/5000_{batch_number}/{sub_batch_name}/failed.csv"
                target_unprocessed_path = results_path / f"{result[1]}/5000_{batch_number}/{sub_batch_name}/unprocesssed.csv"
                

                target_success_path.parent.mkdir(parents=True, exist_ok=True)
                target_failed_path.parent.mkdir(parents=True, exist_ok=True)
                target_unprocessed_path.parent.mkdir(parents=True, exist_ok=True)

                source_success_path = source_path / f"{result_folder_name}/success.csv"
                source_failed_path = source_path / f"{result_folder_name}/failed.csv"
                source_unprocessed_path = source_path / f"{result_folder_name}/unprocessed.csv"

                shutil.copy2(source_success_path, target_success_path)
                shutil.copy2(source_failed_path, target_failed_path)
                shutil.copy2(source_unprocessed_path, target_unprocessed_path)


def insert_missing_id_mappings_for_all(priorities: list[list[str]]):
    for priority in priorities:
        insert_missing_id_mappings(priority)


def insert_missing_id_mappings(priorities: list[str]):
    table_mappings = get_mappings(priorities)
    for table_mapping in table_mappings:
        insert_missing_id_mappings_for_table_mapping(table_mapping)

def insert_missing_id_mappings_for_table_mapping(table_mappings:dict):

    salesforce_object = table_mappings["salesforce_object"]
    transformation_name = table_mappings.get("transformation_name")
    table_name = table_mappings["servicenow_tables"][0]  # Driving table
    job_table = transformation_service.get_id_mapping_table_name(
        table_name,
        table_mappings["salesforce_object"],
        table_mappings.get("transformation_name"),
    )
    id_mapping_table_name = job_table
    
    from helpers.transformation_helper import look_up_sys_id

    results = PATH_TO_RESULTS / f"older_18/{(transformation_name if transformation_name else salesforce_object)}"
    transformation_hash = PATH_TO_TRANSFORMATION / f"hash/{(transformation_name if transformation_name else salesforce_object)}"
    transformation_path = PATH_TO_TRANSFORMATION / f"csv/{(transformation_name if transformation_name else salesforce_object)}"

    total_records = 0
    missing_mappings = []
    if not transformation_path.exists():
        return
    for batch_path in transformation_path.iterdir():
        batch_name = batch_path.stem
        batch_number = batch_name.split("_")[1]
        
        query = """
                    select * from job_table_batches 
                    where job_table_id  in (select id from job_tables where table_name=? and job_id = 3) AND
                    batch_number = ? ORDER BY created_at desc
                """
        
        result = db_client.fetch_one(query, (job_table,batch_number))

        batch_path = results / f"{str(result[1])}/{batch_name}"


        insert_path = next(batch_path.glob("*_insert"), None)
        if not insert_path:
            continue
        success = insert_path / "success.csv"
        total_records = total_records + int(insert_path.stem.split("_")[0])
        
        success_data = read_csv_as_dictionary(success)

        salesforce_hash = convert_to_salesforce_hash(success_data)

        servicenow_hash_path = (
            transformation_hash / f"{batch_name}/{insert_path.stem}_hash.csv"
        )
        mappings = map_servicenow_to_salesforce(servicenow_hash_path, salesforce_hash)

        for sf in salesforce_hash:
            logger.info(f"LatestId: {sf[SALESFORCE_ID_COLUMN]}")
        
        for mapping in mappings:
            salesforce_id = look_up_incident_sys_id("test", mapping["sys_id"])
            if not salesforce_id["test"]:
                missing_mappings.append(mapping)
                logger.info("Batch: %s Missing record for %s. Found mapping: %s", batch_name,mapping["sys_id"], mapping["sf__Id"])

    print(missing_mappings)
    
        # insert_id_mappings(id_mapping_table_name, missing_mappings)


def update_id_mapping_email_table_name():
    mappings = get_mappings(["EmailMessage"]) 
    table_mappings = mappings[0]   
    service_now_tables_path = PATH_TO_DOWNLOADS / "servicenow"
    servicenow_tables = table_mappings["servicenow_tables"]
    driving_table = servicenow_tables[0]
    fields = table_mappings["fields"]
    joins = table_mappings["joins"]
    filters = table_mappings["filters"]

    join_tables = []
    for join_table in servicenow_tables[1:]:
        join_tables.append(service_now_tables_path / join_table)
    base_table = service_now_tables_path / table_mappings["servicenow_tables"][0]
    servicenow_source_fields = transformation_service.extract_unique_source_fields(fields)
    servicenow_source_fields.append(f"{driving_table}.{SERVICE_NOW_ID_COLUMN}")
    service_now_source_data = duckdb_client.fetch_all(
        servicenow_source_fields, base_table, join_tables, joins, filters
    )
    service_now_source_data = service_now_source_data.to_dict(orient='records')
    print(len(service_now_source_data))

    present = 0
    tables = []
    # for row in service_now_source_data:
    #     if not row['target_table'] in tables:
    #         tables.append(row['target_table'])
    # print(tables)
    for row in service_now_source_data:
        sys_id = row['sys_id']
        
        sql = "update id_mappings set table_name =? where servicenow_id =? and table_name=?"
        result = db_client.execute(sql, ("sys_email_EmailMessage_ALLOTHERS",sys_id,"sys_email_EmailMessage_EmailMessage" ))

def id_check_exist():
    ids = ["0nyVt000000W1PQIA0",
            "0nyVt000000WFosIAG",
            "0nyVt000000WGEjIAO",
            "0nyVt000000W1doIAC",
            "0nyVt000000WG6bIAG",
            "0nyVt000000WFouIAG",
            "0nyVt000000WG3QIAW",
            "0nyVt000000WG4tIAG",
            "0nyVt000000WG3TIAW",
            "0nyVt000000WFyaIAG",
            "0nyVt000000WG6gIAG",
            "0nyVt000000WFyXIAW",
            "0nyVt000000WG6cIAG",
            "0nyVt000000WFteIAG",
            "0nyVt000000WG3NIAW",
            "0nyVt000000WG3UIAW",
            "0nyVt000000WGEfIAO",
            "0nyVt000000WG6hIAG",
            "0nyVt000000WFyZIAW",
            "0nyVt000000WGGCIA4",
            "0nyVt000000WG6iIAG",
            "0nyVt000000WFyWIAW",
            "0nyVt000000WFoqIAG",
            "0nyVt000000WFyYIAW",
            "0nyVt000000WG6fIAG",
            "0nyVt000000WG6eIAG",
            "0nyVt000000WFtfIAG",
            "0nyVt000000WG6dIAG",
            "0nyVt000000WFybIAG",
            "0nyVt000000WGEiIAO",
            "0nyVt000000WG4uIAG",
            "0nyVt000000WFtiIAG",
            "0nyVt000000WGGBIA4",
            "0nyVt000000W1hCIAS",
            "0nyVt000000WBzdIAG",
            "0nyVt000000W1HNIA0",
            "0nyVt000000W1O3IAK",
            "0nyVt000000WGJNIA4",
            "0nyVt000000WGMcIAO",
            "0nyVt000000WGHmIAO",
            "0nyVt000000WBy1IAG",
            "0nyVt000000WGZVIA4",
            "0nyVt000000WGfxIAG",
            "0nyVt000000WC1FIAW",
            "0nyVt000000WGcjIAG",
            "0nyVt000000WGUgIAO",
            "0nyVt000000WGT4IAO",
            "0nyVt000000WGhaIAG",
            "0nyVt000000WGRSIA4",
            "0nyVt000000WGeLIAW",
            "0nyVt000000WGWIIA4",
            "0nyVt000000WGb8IAG",
            "0nyVt000000WGPqIAO",
            "0nyVt000000WFAUIA4",
            "0nyVt000000WEkjIAG",
            "0nyVt000000WG3PIAW",
            "0nyVt000000WGXuIAO",
            "0nyVt000000WG3SIAW",
            "0nyVt000000WG3RIAW",
            "0nyVt000000WGEkIAO",
            "0nyVt000000WGElIAO",
            "0nyVt000000WGEmIAO",
            "0nyVt000000WGKzIAO",
            "0nyVt000000WFtgIAG",
            "0nyVt000000WGEgIAO",
            "0nyVt000000WFopIAG",
            "0nyVt000000WGEhIAO",
            "0nyVt000000WFycIAG",
            "0nyVt000000WForIAG",
            "0nyVt000000WG3OIAW",
            "0nyVt000000WFthIAG",
            "0nyVt000000WFotIAG",
            "0nyVt000000W4DNIA0",
            "0nyVt000000WLhFIAW",
            "0nyVt000000W8SHIA0",
            "0nyVt000000VxmzIAC",
            "0nyVt000000W8aLIAS",
            "0nyVt000000W2TJIA0",
            "0nyVt000000VxztIAC",
            "0nyVt000000W8iPIAS",
            "0nyVt000000WLirIAG"]
    

    for id in ids:
        sql = "select * from id_mappings where salesforce_id=? and table_name=?"
        result = db_client.fetch_one(sql, (id, "incident_Incident_ParentIncident"))
        if not result: 
           print(f"id not exists {id}") 

def get_missing_incident_ids():
    incident_path = PATH_TO_DOWNLOADS / f"salesforce/Incident"
    sql = "select salesforce_id, servicenow_id from id_mappings where  table_name=?"
    result = db_client.fetch_all(sql, ( "incident_Incident_ParentIncident",))
    sf_ids = {row[0]:row[1] for row in result }
    all_not_mapped_subjects = []
    ids_available_in_sf = []
    for batch_path in incident_path.iterdir():
        batch = read_csv_as_dictionary(batch_path)
        for row in batch:
            sf_id = row['Id']
            ids_available_in_sf.append(sf_id)
            subject = row['Subject'].split("-")[0]
            is_parent = not bool(row['ParentIncidentId'])

            if sf_id in ['0nyVt000000WG1eIAG', '0nyVt000000WG9hIAG', '0nyVt000000WOR5IAO', '0nyVt000000WEhYIAW']:
                print("Here")

            if not sf_id in sf_ids and  is_parent:
                all_not_mapped_subjects.append(
                    {
                        "sf_id":sf_id,
                        "incident_number": subject.strip()
                    }
                )

    ids_to_delete = [id for id in sf_ids  if not id in ids_available_in_sf]

    mappings = get_service_now_id(all_not_mapped_subjects)

    final_mappings = []

    sf_ids = [
        "0nyVt000000W1PQIA0",
"0nyVt000000WEb4IAG",
"0nyVt000000WGEjIAO",
"0nyVt000000W1doIAC",
"0nyVt000000WG6bIAG",
"0nyVt000000WEb6IAG",
"0nyVt000000WG3QIAW",
"0nyVt000000WG4tIAG",
"0nyVt000000WEzLIAW",
"0nyVt000000WEo0IAG",
"0nyVt000000WF4AIAW",
"0nyVt000000WFyXIAW",
"0nyVt000000WG6cIAG",
"0nyVt000000WFteIAG",
"0nyVt000000WEzFIAW",
"0nyVt000000WG3UIAW",
"0nyVt000000WGEfIAO",
"0nyVt000000WF4BIAW",
"0nyVt000000WFyZIAW",
"0nyVt000000WFLkIAO",
"0nyVt000000WF4CIAW",
"0nyVt000000WFyWIAW",
"0nyVt000000WEb2IAG",
"0nyVt000000WEnyIAG",
"0nyVt000000WG6fIAG",
"0nyVt000000WF48IAG",
"0nyVt000000WFtfIAG",
"0nyVt000000WG6dIAG",
"0nyVt000000WEo1IAG",
"0nyVt000000WFKGIA4",
"0nyVt000000WF0mIAG",
"0nyVt000000WFtiIAG",
"0nyVt000000WGGBIA4",
"0nyVt000000VYO8IAO",
"0nyVt000000WBzdIAG",
"0nyVt000000V1deIAC",
"0nyVt000000VDwEIAW",
"0nyVt000000WGJNIA4",
"0nyVt000000WFTmIAO",
"0nyVt000000WGHmIAO",
"0nyVt000000WAU5IAO",
"0nyVt000000WCuBIAW",
"0nyVt000000WGCvIAO",
"0nyVt000000WAXJIA4",
"0nyVt000000WDALIA4",
"0nyVt000000WEj6IAG",
"0nyVt000000WEXoIAO",
"0nyVt000000WGBKIA4",
"0nyVt000000WCrKIAW",
"0nyVt000000WG9hIAG",
"0nyVt000000WEhYIAW",
"0nyVt000000WG1eIAG",
"0nyVt000000WFlWIAW",
"0nyVt000000WDGvIAO",
"0nyVt000000WCq8IAG",
"0nyVt000000WEzHIAW",
"0nyVt000000WEmMIAW",
"0nyVt000000WG3SIAW",
"0nyVt000000WEzJIAW",
"0nyVt000000WFKIIA4",
"0nyVt000000WGElIAO",
"0nyVt000000WFKKIA4",
"0nyVt000000WGKzIAO",
"0nyVt000000WFtgIAG",
"0nyVt000000WGEgIAO",
"0nyVt000000WEb1IAG",
"0nyVt000000WFKFIA4",
"0nyVt000000WEo2IAG",
"0nyVt000000WEb3IAG",
"0nyVt000000WEzGIAW",
"0nyVt000000WEeHIAW",
"0nyVt000000WEb5IAG",
"0nyVt000000W4DNIA0",
"0nyVt000000W8SHIA0",
"0nyVt000000VxmzIAC",
"0nyVt000000W8aLIAS",
"0nyVt000000W2TJIA0",
"0nyVt000000VxztIAC",
"0nyVt000000W8iPIAS",

    ]

    for sf_id in sf_ids:
        for mapping in mappings:
            if mapping['sf__Id'] == sf_id:
                final_mappings.append(mapping)

    final_mapping_servicenow_keys = [final_mapping['sys_id'] for final_mapping in final_mappings]
    
    for mapping in mappings:

        if mapping['sys_id'] not in final_mapping_servicenow_keys:
            final_mappings.append(mapping)
        else:
            print(f"Found duplicate {mapping['sys_id']}")
    delete_id_mappings('incident_Incident_ParentIncident', [mapping['sys_id'] for mapping in final_mappings])
    insert_id_mappings("incident_Incident_ParentIncident", final_mappings)



def get_service_now_id(sf_with_number):
    inc_number = [r['incident_number'].strip() for r in sf_with_number]
    path = PATH_TO_DOWNLOADS / f"servicenow/incident"
    fields = ['sys_id', 'number']
    results = duckdb_client.fetch_all(
        fields, path, [],[],[
            {
                "table": "incident",
                "field": "number",
                "operator": "in",
                "value": inc_number
            }
        ]
    )
    results = results.to_dict(orient='records')
    mappings = []
    for result in results:
        for r in sf_with_number:
            if r['incident_number'] == result['number']:
                mappings.append(
                    {
                        "sf__Id": r["sf_id"],
                        "sys_id": result['sys_id']
                    }
                )
    return mappings



def delete_records():
    path = PATH_TO_DATA / f"to_delete/delete.csv"
    job_id = salesforce_client.create_job("Incident", "delete");

    salesforce_client.upload_csv(job_id,path)
    salesforce_client.mark_upload_complete(job_id)


    terminal_states = {"JobComplete", "Failed", "Aborted"}
    job_satus = None
    while True:
        job_satus = salesforce_client.get_job(job_id)
        state = job_satus.get("state")
        if state == "Failed":
            logger.info(job_satus)

        if state in terminal_states:
            break
        time.sleep(POLL_TIME)  # backoff as needed

    if job_satus:
        processed_records = job_satus.get("numberRecordsProcessed")
        failed_records = job_satus.get("numberRecordsFailed")

        print(failed_records)
        print(processed_records)
       

        
def delete_id_mapping_entries():
    csv_to_delete = PATH_TO_DATA / f"to_delete/id_mapping_salesforce_id.csv"
    data = read_csv_as_dictionary(csv_to_delete)
    to_delete = [f"'{row['Id']}'" for row in data]

    parent_table_name = "incident_ChangeRequestRelatedIssue_RelatedCRIncidentByRFC"
    sql = f"delete from id_mappings where table_name=? and salesforce_id in ({",".join(to_delete)})"
    db_client.execute(sql, (parent_table_name,))


def find_attachments_for_incidents():
    incident_id_path = PATH_TO_DATA / f"missing_attachents/missing_incident_attachments.csv"
    column_name = 'id'

    salesforce_ids:list[str] = []
    with open(incident_id_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            id = row.get(column_name)
            if id:
                salesforce_ids.append(id)
    
    placeholders = ",".join(["?"] * len(salesforce_ids))

    sql = f"""
        SELECT servicenow_id
        FROM id_mappings
        WHERE table_name IN (?, ?)
          AND salesforce_id IN ({placeholders})
    """

    params = (
        "incident_Incident_ParentIncident",
        "incident_Incident_ChildIncident",
        *salesforce_ids
    )

    snow_ids = db_client.fetch_all(sql, params)

    print(snow_ids)


def get_snow_data():
    path = PATH_TO_DOWNLOADS / f"servicenow/incident"
    fields = ['sys_id']
    results = duckdb_client.fetch_all(
        fields, path, [],[],[
            {
                "table": "incident",
                "field": "caller_id",
                "operator": "equals",
                "value": "4243d7331bcf0410811f748edc4bcb3d"
            }
        ]
    )
    results = results.to_dict(orient='records')
    count = len(results)
    print(f"Count : {count}")
            
