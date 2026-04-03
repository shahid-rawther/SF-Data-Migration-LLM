from config.settings import (
    MAPPING_PRIORITY,
    PATH_TO_DATA,
    PATH_TO_DOWNLOADS,
    PATH_TO_TRANSFORMATION,
    SALESFORCE_EXTRACTION_JOB_NUMBER,
    SERVICE_NOW_ID_COLUMN,
)
from helpers.hash_helper import create_hash_for_row
from helpers.transformation_helper import convert_to_sf_time_format, look_up_case_sys_id, look_up_product, look_up_service_request_sys_id, look_up_variable
from services.external import duckdb_client
from services.external.db_client import db_client
from services.internal import extraction_service, transformation_service
from services.internal.mappings_service import (
    delete_id_mappings_by_salesforce_id,
    get_id_mapping_table_name,
    get_mappings,
)
from tools.csv_manager import read_csv_as_dictionary, save_as_csv


def get_missing_incident_ids(priorities: list[list[dict]]):
    missing_ids = {}
    for priority in priorities:
        all_table_mappings = get_mappings(priority)

        for table_mappings in all_table_mappings:

            salesforce_object = table_mappings["salesforce_object"]
            salesforce_objects = table_mappings.get("salesforce_objects")
            transformation_name = table_mappings.get("transformation_name")
            table_name = table_mappings["servicenow_tables"][0]  # Driving table
            job_table = transformation_service.get_id_mapping_table_name(
                table_name,
                table_mappings["salesforce_object"],
                transformation_name,
            )
            id_mapping_table_name = job_table
            incident_path = PATH_TO_DOWNLOADS / f"salesforce/{salesforce_object}"

            if salesforce_objects:
                print(f"Extracting object {salesforce_object}")
                extraction_service.extract_salesforce_objects(
                    job_id=SALESFORCE_EXTRACTION_JOB_NUMBER,
                    sales_force_objects=salesforce_objects,
                )
                print(f"Extracting object {salesforce_object}...Done")

            sql = "select salesforce_id, servicenow_id from id_mappings where  table_name=?"
            result = db_client.fetch_all(sql, (id_mapping_table_name,))
            sf_ids = [row[0] for row in result]
            sf_ids_set = set(sf_ids)
            ids_available_in_sf = []
            ids_to_delete = []
            for batch_path in incident_path.iterdir():
                batch = read_csv_as_dictionary(batch_path)
                for row in batch:
                    sf_id = row["Id"]
                    ids_available_in_sf.append(sf_id)

            ids_available_in_sf_set = set(ids_available_in_sf)
            ids_to_delete = list(sf_ids_set - ids_available_in_sf_set)

            # if len(ids_to_delete) == 0 :
            #     continue
            # with open(delete_file_path, "w") as f:
            #     f.write(",".join(ids_to_delete))
            #     f.write(f"ids_available_in_sf: {len(ids_available_in_sf)}")
            #     f.write(f"ids_available_in_id_mapping: {len(sf_ids)}")
            #     f.write(f"ids_to_delete: {len(ids_to_delete)}")

            missing_ids[id_mapping_table_name] = ids_to_delete
    return missing_ids


def get_missing_salesforce_ids(priorities: list[list[dict]]):
    missing_ids = {}
    for priority in priorities:
        all_table_mappings = get_mappings(priority)

        for table_mappings in all_table_mappings:

            salesforce_object = table_mappings["salesforce_object"]
            salesforce_objects = table_mappings.get("salesforce_objects")
            transformation_name = table_mappings.get("transformation_name")
            table_name = table_mappings["servicenow_tables"][0]  # Driving table
            job_table = transformation_service.get_id_mapping_table_name(
                table_name,
                table_mappings["salesforce_object"],
                transformation_name,
            )
            id_mapping_table_name = job_table
            incident_path = PATH_TO_DOWNLOADS / f"salesforce/{salesforce_object}"


            if salesforce_objects:
                print(f"Extracting object {salesforce_object}")
                extraction_service.extract_salesforce_objects(
                    job_id=SALESFORCE_EXTRACTION_JOB_NUMBER,
                    sales_force_objects=salesforce_objects,
                )
                print(f"Extracting object {salesforce_object}...Done")

            sql = "select salesforce_id, servicenow_id from id_mappings where  table_name=?"
            result = db_client.fetch_all(sql, (id_mapping_table_name,))
            sf_ids = [row[0] for row in result]
            sf_ids_set = set(sf_ids)
            ids_available_in_sf = []
            salesforce_ids_to_add = []
            for batch_path in incident_path.iterdir():
                batch = read_csv_as_dictionary(batch_path)
                for row in batch:
                    sf_id = row["Id"]
                    ids_available_in_sf.append(sf_id)

            
            ids_available_in_sf_set = set(ids_available_in_sf)
            salesforce_ids_to_add = list(ids_available_in_sf_set - sf_ids_set)
            missing_ids[id_mapping_table_name] = salesforce_ids_to_add
            
    return missing_ids
def get_sys_id_for_sf_ids_by_table_name(sf_ids, table_name):
    results = []
    BATCH_SIZE = 20000
    for i in range(0, len(sf_ids), BATCH_SIZE):
        sub_batch = sf_ids[i : i + BATCH_SIZE]
        placeholders = ",".join(["?"] * len(sub_batch))
        sql = f"select servicenow_id from id_mappings where table_name = ? and salesforce_id in ({placeholders})"
        params = [table_name, *sub_batch]
        results.extend(db_client.fetch_all(sql, params))
    return [row[0] for row in results]


def check_before_delete_if_sf_id_exists():
    missing_ids = get_missing_incident_ids(MAPPING_PRIORITY)
    f = open((PATH_TO_DATA / f"report/missing_id_count.txt"), "w")
    for priority in MAPPING_PRIORITY:
        mappings = get_mappings(priority)
        for table_mappings in mappings:
            service_now_tables_path = PATH_TO_DOWNLOADS / "servicenow"
            salesforce_tables_path = PATH_TO_DOWNLOADS / "salesforce"
            servicenow_tables = table_mappings["servicenow_tables"]
            salesforce_object_name = table_mappings["salesforce_object"]
            driving_table = servicenow_tables[0]
            fields = table_mappings["fields"]
            joins = table_mappings["joins"]
            transformation_name = table_mappings.get("transformation_name")
            id_mapping_table_name = get_id_mapping_table_name(
                driving_table, salesforce_object_name, transformation_name
            )
            missing_id_length = len(missing_ids[id_mapping_table_name])
            print(f"{id_mapping_table_name}: {missing_id_length}")
            f.write(f"{id_mapping_table_name}: {missing_id_length}\n")
            if missing_id_length == 0:
                continue
            join_tables = []
            for join_table in servicenow_tables[1:]:
                join_tables.append(service_now_tables_path / join_table)
            base_table = (
                service_now_tables_path / table_mappings["servicenow_tables"][0]
            )
            servicenow_source_fields = (
                transformation_service.extract_unique_source_fields(fields)
            )
            servicenow_source_fields.append(f"{driving_table}.{SERVICE_NOW_ID_COLUMN}")
            filters = [
                {
                    "table": driving_table,
                    "field": "sys_id",
                    "operator": "in",
                    "value": get_sys_id_for_sf_ids_by_table_name(
                        missing_ids[id_mapping_table_name], id_mapping_table_name
                    ),
                }
            ]

            service_now_source_data = duckdb_client.fetch_all(
                servicenow_source_fields, base_table, join_tables, joins, filters
            ).to_dict(orient="records")
            path = salesforce_tables_path / f"{salesforce_object_name}"
            if salesforce_object_name in (
                "Incident",
                "ChangeRequest",
                "Case",
                "Probelm",
            ):
                mappings = find_match_by_subject(service_now_source_data, path)
            elif salesforce_object_name in ("EmailMessage"):
                mappings = find_match_by_header(service_now_source_data, path)
            elif salesforce_object_name in ("FeedItem"):
                mappings = find_match_by_body(service_now_source_data, path)
            elif salesforce_object_name in ["SvcCatalogRequest"]:
                mappings = find_match_for_service_request(service_now_source_data, path)

            print(
                f"Found matching mappings for {id_mapping_table_name}:{len(mappings)}"
            )

    f.close()


def find_match_by_subject(service_now_source_data, path, adding_ids_length):
    mappings = []

    look_up = {}
    for batch_path in path.iterdir():
        batch = read_csv_as_dictionary(batch_path)
        for row in batch:
            subject = row["Subject"]
            sf_subject_number = subject.split("-")[0].strip()
            if row["Id"] in adding_ids_length:
                look_up[sf_subject_number] = row["Id"]

    for sn_row in service_now_source_data:
        number = sn_row["number"]
        if number in look_up:
            mappings.append({"sf__Id": look_up[number], "sys_id": sn_row["sys_id"]})
        
    return mappings


def find_match_for_service_request(service_now_source_data, path):
    mappings = []
    
    for sn_row in service_now_source_data:
        short_description = sn_row["short_description"]
        name = sn_row["name"]
        product_id = look_up_product("test",sn_row["cat_item"])["test"]
        case_id = look_up_case_sys_id("test", sn_row["sys_id"])["test"]
        for batch_path in path.iterdir():
            batch = read_csv_as_dictionary(batch_path)
            for row in batch:
                Id = row["Id"]
                reference_object_id = row["ReferenceObjectId"]
                catalog_item_name = row["CatalogItemName"]
                catalog_item_description = row["CatalogItemDescription"]
                sr_product = row["ProductId"]

                if (
                    sr_product == product_id and
                    case_id == reference_object_id
                    and name == catalog_item_name
                    and catalog_item_description == short_description
                ):
                    mappings.append({"sf__Id": row["Id"], "sys_id": sn_row["sys_id"]})

    return mappings


def find_match_for_service_request_attr_val(service_now_source_data, path):
    mappings = []
    look_up = {}
    for batch_path in path.iterdir():
        batch = read_csv_as_dictionary(batch_path)
        for row in batch:
            catalog_id = row["SvcCatalogRequestId"]
            attribute_id = row["SvcCatalogItemAttributeId"]
            key = f"{catalog_id}{attribute_id}"
            look_up[key] = row["Id"]

    for sn_row in service_now_source_data:
        request_item = look_up_service_request_sys_id("test",sn_row["request_item"])["test"]
        value = sn_row["value"]
        item_option_new = look_up_variable("test", sn_row["item_option_new"])["test"]
        key = f"{request_item}{item_option_new}"
        if key in look_up:
            mappings.append({"sf__Id": look_up[key], "sys_id": sn_row["sys_id"]})

        
    return mappings


def find_match_by_header(service_now_source_data, path, adding_ids_length):
    header_lookup = {
        sn_row["headers"]: sn_row["sys_id"] for sn_row in service_now_source_data
    }
    mappings = []
    adding_ids_length_set = set(adding_ids_length)
    for batch_path in path.iterdir():
        batch = read_csv_as_dictionary(batch_path)
        for row in batch:
            if row["Id"] in adding_ids_length_set:
                subject = row["Headers"]
                if subject in header_lookup:
                    mappings.append({"sf__id": row["Id"], "sys_id": header_lookup[subject]})

    return mappings


def find_match_by_body(service_now_source_data, path):
    header_lookup = {
        create_hash_for_row(
            {
                "value": sn_row["value"],
                "created_date": convert_to_sf_time_format(
                    "test", sn_row["sys_created_on"]
                )["test"],
            }
        ): sn_row["sys_id"]
        for sn_row in service_now_source_data
    }
    mappings = []
    for batch_path in path.iterdir():
        batch = read_csv_as_dictionary(batch_path)
        for row in batch:
            date = row["CreatedDate"]
            subject = row["Body"]
            hash = create_hash_for_row({"value": subject, "created_date": date})
            if hash in header_lookup:
                mappings.append({"sf__id": row["Id"], "sys_id": header_lookup[subject]})

    return mappings


def get_sn_to_sf_mapping_by_subject(table, sf_with_number):
    inc_number = [r["number"].strip() for r in sf_with_number]
    path = PATH_TO_DOWNLOADS / f"servicenow/{table}"
    fields = ["sys_id", "number"]
    results = duckdb_client.fetch_all(
        fields,
        path,
        [],
        [],
        [{"table": table, "field": "number", "operator": "in", "value": inc_number}],
    )
    results = results.to_dict(orient="records")
    mappings = []
    for result in results:
        for r in sf_with_number:
            if r["incident_number"] == result["number"]:
                mappings.append({"sf__Id": r["sf_id"], "sys_id": result["sys_id"]})
    return mappings


def clean_up_case():
    case_object_ids = [
        "500Vt00001586JiIAI",
        "500Vt00001585wVIAQ",
        "500Vt00001585ksIAA",
        "500Vt00001587H7IAI",
        "500Vt00001585mQIAQ",
        "500Vt00001585LeIAI",
        "500Vt000015889rIAA",
        "500Vt00001585dZIAQ",
        "500Vt00001586R4IAI",
        "500Vt00001587rBIAQ",
        "500Vt00001588plIAA",
        "500Vt00001587L0IAI",
        "500Vt000015888iIAA",
        "500Vt00001587qiIAA",
        "500Vt00001588KtIAI",
        "500Vt000015869RIAQ",
        "500Vt00001585mRIAQ",
        "500Vt00001588OUIAY",
        "500Vt00001588K5IAI",
        "500Vt00001587s8IAA",
        "500Vt00001588bAIAQ",
        "500Vt00001587M0IAI",
        "500Vt00001585u1IAA",
        "500Vt00001587cqIAA",
        "500Vt00001588K3IAI",
        "500Vt00001588LAIAY",
        "500Vt00001585dcIAA",
        "500Vt00001587UxIAI",
        "500Vt00001587HYIAY",
        "500Vt00001586x0IAA",
        "500Vt00001585iOIAQ",
        "500Vt00001588pnIAA",
        "500Vt0000158870IAA",
        "500Vt00001588poIAA",
        "500Vt00001588KeIAI",
        "500Vt00001588pmIAA",
        "500Vt00001585LfIAI",
        "500Vt00001588K7IAI",
        "500Vt00001588b5IAA",
        "500Vt00001586vJIAQ",
        "500Vt00001586vUIAQ",
        "500Vt00001588K0IAI",
        "500Vt00001586vKIAQ",
        "500Vt00001585wTIAQ",
        "500Vt00001587cfIAA",
        "500Vt00001587UuIAI",
        "500Vt00001586AvIAI",
        "500Vt00001585JyIAI",
        "500Vt00001588a8IAA",
        "500Vt00001585mSIAQ",
        "500Vt00001588b4IAA",
        "500Vt00001586mSIAQ",
        "500Vt00001588IbIAI",
        "500Vt00001587HAIAY",
        "500Vt00001586vHIAQ",
    ]

    path_to_success = PATH_TO_DATA / f"fix/case_fix"

    for batch_path in path_to_success.iterdir():
        subject_ids = {}
        success_data = read_csv_as_dictionary(batch_path)
        for row in success_data:
            sf_id = row.pop("sf__Id")
            _ = row.pop("sf__Created")
            subject = create_hash_for_row(row)
            if subject in subject_ids:
                subject_ids[subject].append(sf_id)
            else:
                subject_ids[subject] = [sf_id]
    
        duplicates = {}

    for salesforce_id in case_object_ids:
        for subject in subject_ids:
            if salesforce_id in subject_ids[subject]:
                duplicate_records = subject_ids[subject].copy()
                duplicate_records.remove(salesforce_id)
                duplicates[salesforce_id] = duplicate_records
    for duplicate in duplicates:
        for salesforce_id in duplicates[duplicate]:
            sql = "select id from id_mappings where salesforce_id=? and table_name = ?"
            result = db_client.fetch_one(sql, (duplicate,'sc_req_item_Case_InsertCase'))
            if result:
                sql = "select id from id_mappings where salesforce_id=? and table_name = ?"
                is_exists = db_client.fetch_one(sql, (salesforce_id,'sc_req_item_Case_InsertCase'))
                if not is_exists:
                    sql = "update id_mappings set salesforce_id = ? where id = ? and table_name = ?"
                    db_client.execute(sql, (salesforce_id,result[0], 'sc_req_item_Case_InsertCase'))



def clean_up_case_v2():
    sql = "select salesforce_id from id_mappings where table_name =? group by salesforce_id having count() > 1"
    result = db_client.fetch_all(sql, ('sc_req_item_Case_InsertCase',))
    salesforce_ids = [row[0] for row in result]
    path_to_success = PATH_TO_DATA / f"fix/case_fix" 

    

    for batch_path in path_to_success.iterdir():
        subject_ids = {}
        success_data = read_csv_as_dictionary(batch_path)
        for row in success_data:
            sf_id = row.pop("sf__Id")
            _ = row.pop("sf__Created")
            subject = create_hash_for_row(row)
            if subject in subject_ids:
                subject_ids[subject].append(sf_id)
            else:
                subject_ids[subject] = [sf_id]
    
    duplicates = {}

    for salesforce_id in salesforce_ids:
        for subject in subject_ids:
            if salesforce_id in subject_ids[subject]:
                duplicate_records = subject_ids[subject].copy()
                duplicate_records.remove(salesforce_id)
                duplicates[salesforce_id] = duplicate_records
    # for sf_id in duplicates:
    #     print(f"Duplicates found: {len(duplicates[sf_id])}")
    
    for duplicate in duplicates:
        for salesforce_id in duplicates[duplicate]:
            sql = "select id from id_mappings where salesforce_id=?"
            result = db_client.fetch_one(sql, (duplicate,))
            if not result:
                print(f"Entry not found: {duplicate}")
                continue
            sql = "update id_mappings set salesforce_id = ? where id = ?"
            db_client.execute(sql, (salesforce_id,result[0]))


def insert_missing_mappings():
    ids_to_add = get_missing_salesforce_ids(MAPPING_PRIORITY)
    f = open((PATH_TO_DATA / f"report/missing_id_count.txt"), "w")
    for priority in MAPPING_PRIORITY:
        mappings = get_mappings(priority)
        for table_mappings in mappings:
            service_now_tables_path = PATH_TO_DOWNLOADS / "servicenow"
            salesforce_tables_path = PATH_TO_DOWNLOADS / "salesforce"
            servicenow_tables = table_mappings["servicenow_tables"]
            salesforce_object_name = table_mappings["salesforce_object"]
            driving_table = servicenow_tables[0]
            fields = table_mappings["fields"]
            joins = table_mappings["joins"]
            transformation_name = table_mappings.get("transformation_name")
            id_mapping_table_name = get_id_mapping_table_name(
                driving_table, salesforce_object_name, transformation_name
            )
            adding_ids_length = len(ids_to_add[id_mapping_table_name])
            if adding_ids_length == 0:
                continue
            join_tables = []
            for join_table in servicenow_tables[1:]:
                join_tables.append(service_now_tables_path / join_table)
            base_table = (
                service_now_tables_path / table_mappings["servicenow_tables"][0]
            )
            servicenow_source_fields = (
                transformation_service.extract_unique_source_fields(fields)
            )
            servicenow_source_fields.append(f"{driving_table}.{SERVICE_NOW_ID_COLUMN}")

            # sql = "select servicenow_id from id_mappings where table_name=?"
            # result = db_client.fetch_all(sql, (id_mapping_table_name,))
            # sys_ids = [row[0] for row in result]

            # filters = [
            #     {
            #         "table": driving_table,
            #         "field": "sys_id",
            #         "operator": "not_in",
            #         "value": sys_ids
            #     }
            # ]

            service_now_source_data = duckdb_client.fetch_all(
                servicenow_source_fields, base_table, join_tables, joins, []
            ).to_dict(orient="records")


            print(f"Total source data: {len(service_now_source_data)}")
            path = salesforce_tables_path / f"{salesforce_object_name}"
            if salesforce_object_name in (
                "Incident",
                "ChangeRequest",
                "Case",
                "Probelm",
            ):
                mappings = find_match_by_subject(service_now_source_data, path, ids_to_add[id_mapping_table_name])
            elif salesforce_object_name in ("EmailMessage"):
                mappings = find_match_by_header(service_now_source_data, path, ids_to_add[id_mapping_table_name])
            elif salesforce_object_name in ("FeedItem"):
                mappings = find_match_by_body(service_now_source_data, path)
            elif salesforce_object_name in ["SvcCatalogRequest"]:
                mappings = find_match_for_service_request(service_now_source_data, path)
            elif salesforce_object_name in ["SvcCatalogReqExtdAttrVal"]:
                mappings = find_match_for_service_request_attr_val(service_now_source_data, path)

            print(
                f"Found matching mappings for {id_mapping_table_name}:{len(mappings)}"
            )
            # delete_id_mappings(id_mapping_table_name, [mapping['sys_id'] for mapping in mappings])
            # insert_id_mappings(id_mapping_table_name, mappings)
    f.close()


def remove_salesforce_ids_not_in_salesforce(priorities: list[list[str]]):
    missing_ids = {}
    for priority in priorities:
        all_table_mappings = get_mappings(priority)

        for table_mappings in all_table_mappings:

            salesforce_object = table_mappings["salesforce_object"]
            salesforce_objects = table_mappings.get("salesforce_objects")
            transformation_name = table_mappings.get("transformation_name")
            table_name = table_mappings["servicenow_tables"][0]  # Driving table
            job_table = transformation_service.get_id_mapping_table_name(
                table_name,
                table_mappings["salesforce_object"],
                transformation_name,
            )
            id_mapping_table_name = job_table
            incident_path = PATH_TO_DOWNLOADS / f"salesforce/{salesforce_object}"


            # if salesforce_objects:
            #     print(f"Extracting object {salesforce_object}")
            #     extraction_service.extract_salesforce_objects(
            #         job_id=SALESFORCE_EXTRACTION_JOB_NUMBER,
            #         sales_force_objects=salesforce_objects,
            #     )
            #     print(f"Extracting object {salesforce_object}...Done")

            sql = "select salesforce_id, servicenow_id from id_mappings where  table_name=?"
            result = db_client.fetch_all(sql, (id_mapping_table_name,))
            sf_ids = [row[0] for row in result]
            sf_ids_set = set(sf_ids)
            ids_available_in_sf = []
            salesforce_ids_to_add = []
            for batch_path in incident_path.iterdir():
                batch = read_csv_as_dictionary(batch_path)
                for row in batch:
                    sf_id = row["Id"]
                    ids_available_in_sf.append(sf_id)
            ids_available_in_sf_set = set(ids_available_in_sf)
            
            ids_to_remove = sf_ids_set - ids_available_in_sf_set
            missing_ids[id_mapping_table_name] = ids_to_remove
            
    for table_name in missing_ids:
        print(table_name)
        delete_id_mappings_by_salesforce_id(table_name, [id  for id in missing_ids[table_name]])

def create_count():
    report = {}
    failed_report_dir = PATH_TO_DATA / f"failed_records"
    for priority in MAPPING_PRIORITY:
        mappings = get_mappings(priority)
        for table_mappings in mappings:
            servicenow_tables = table_mappings["servicenow_tables"]
            salesforce_object_name = table_mappings["salesforce_object"]
            driving_table = servicenow_tables[0]
            transformation_name = table_mappings.get("transformation_name")
            id_mapping_table_name = get_id_mapping_table_name(
                driving_table, salesforce_object_name, transformation_name
            )
            salesforce_tables_path = PATH_TO_TRANSFORMATION / f"csv/{(transformation_name if transformation_name else salesforce_object_name)}"
            records = []
            if salesforce_tables_path.exists():
                total_records = 0
                for batch_path in salesforce_tables_path.iterdir():
                    if batch_path.is_dir():
                        for insert_file in batch_path.iterdir():
                            records.extend(read_csv_as_dictionary(insert_file))
                folder_name = (transformation_name if transformation_name else salesforce_object_name)
                path = failed_report_dir / f"{folder_name}/records.csv"
                save_as_csv(path, records)
                report[id_mapping_table_name] = len(records)
    for table_name in report:
        print(f"{table_name}: {report[table_name]}")
