import dotenv

dotenv.load_dotenv()

from controllers import extraction_controller
from controllers import transformation_controller
from controllers import loading_controller
from tools.partition import do_partition
from config.settings import (
    ATTACHMENT_EXTRACTION_JOB_NUMBER,
    ATTACHMENT_LOADING_JOB_NUMBER,
    ATTACHMENT_TRANSFORMATION_JOB_NUMBER,
    EXTRACTION_JOB_NUMBER,
    ATTACHMENT_MAPPING_PRIORITY,
    TRANSFORMATION_JOB_NUMBER,
    LOADING_JOB_NUMBER,
    MAPPING_PRIORITY,
)

import argparse


def main():
    parser = argparse.ArgumentParser(description="Data Migration App CLI")
    parser.add_argument(
        "--op",
        choices=["extract-tables", "extract-attachments", "extract-all-tables", "transform-load", "transform-load-attachments"],
        required=True,
        help="Operation to run",
    )
    args = parser.parse_args()

    if args.op == "extract-tables":
        extract_tables(MAPPING_PRIORITY)
    if args.op == "extract-all-tables":
        extraction_controller.extract_full_tables()
    elif args.op == "extract-attachments":
        extract_attachments(ATTACHMENT_MAPPING_PRIORITY)
    elif args.op == "transform-load":
        transform_and_load(MAPPING_PRIORITY)
    elif args.op == "transform-load-attachments":
        transform_attachments_and_load(ATTACHMENT_MAPPING_PRIORITY)


def extract_tables(priorities: list):
    mapping_file_names = [item for sublist in priorities for item in sublist]
    extraction_controller.start_extraction(
        EXTRACTION_JOB_NUMBER, mapping_file_names
    )

def extract_attachments(priorities: list):
    for priority in priorities:
        extraction_controller.start_attachment_table_extraction(ATTACHMENT_EXTRACTION_JOB_NUMBER, priority)
    do_partition()

def transform_attachments_and_load(priorities: list):
    for priority in priorities:
        transformation_controller.start_trasformation(
            ATTACHMENT_TRANSFORMATION_JOB_NUMBER, priority
        )
        loading_controller.start_loading(ATTACHMENT_LOADING_JOB_NUMBER, priority)

def transform_and_load(priorities: list):
    for priority in priorities:
        transformation_controller.start_trasformation(
            TRANSFORMATION_JOB_NUMBER, priority
        )
        loading_controller.start_loading(LOADING_JOB_NUMBER, priority)
        continue

if __name__ == "__main__":
    main()



