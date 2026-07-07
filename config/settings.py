from pathlib import Path

PATH_TO_DATA = Path(__file__).resolve().parent.parent.absolute() / "data"
PATH_TO_DOWNLOADS = Path(__file__).resolve().parent.parent.absolute() / "data/downloads"
PATH_TO_TRANSFORMATION = (
    Path(__file__).resolve().parent.parent.absolute() / "data/transformations"
)
PATH_TO_RESULTS = Path(__file__).resolve().parent.parent.absolute() / "data/results"
PATH_TO_MAPPING = (
    Path(__file__).resolve().parent.parent.absolute() / "integrations/servicenow/mappings"
)
PATH_TO_JSM_MAPPING = (
    Path(__file__).resolve().parent.parent.absolute() / "integrations/jira/mappings"
)
PATH_TO_PRIORITY_MAPPING = (
    Path(__file__).resolve().parent.parent.absolute() / "data/mappings/priority"
)

# Salesforce Service_now_id_column
SERVICE_NOW_ID_COLUMN = "sys_id"
HASH_KEY_NAME = "hash_key"
SALESFORCE_ID_COLUMN = "sf__Id"
SALESFORCE_CREATED_AT_COLUMN = "sf__Created"

# Salesforce batch
SALESFORCE_BATCH_SIZE = 1000

# Service Now Batch
SERVICE_NOW_NO_OF_BATCH = None
SERVICE_NOW_BATCH_SIZE = 10000
SERVICE_NOW_START_DATE = ""
SERCICE_NOW_END_DATE = ""

# JSM (Jira Service Management) Batch
JSM_BATCH_SIZE = 100  # Jira search API caps at 100 per request

# Jobs
EXTRACTION_JOB_NUMBER = 1
TRANSFORMATION_JOB_NUMBER = 2
LOADING_JOB_NUMBER = 3
ATTACHMENT_EXTRACTION_JOB_NUMBER = 4
ATTACHMENT_TRANSFORMATION_JOB_NUMBER = 7
ATTACHMENT_LOADING_JOB_NUMBER = 8
SALESFORCE_EXTRACTION_JOB_NUMBER = 6


# Attachment loading batch size
LOADING_BATCH_SIZE = 25


# Polling Time in Sec
POLL_TIME = 3


# Thread Pooling
MAX_WORKERS = 10

# Priority
MAPPING_PRIORITY = [
    ["Group"],
    [
        "QueueCase",
        "QueueCaseServiceProcess",
        "QueueChangeRequest", 
        "QueueIncident",
        "QueueProblem", 
        "QueueSvcCatalogRequest"
    ],
    ["AdminUser"],
    ["Account"],
    ["CommunityUser"],
    ["GroupMember"],
    ['UpdateUserForManager'],
    ["ParentLocation"],
    ["Department"],
    ["Address"],
    ["Company"],
    ["ParentLocationUpdateForVisitorAddressId"],
    ["UpdateAddressForStreet"],
    ["UpdateProblemForFix"],
    ["UpdateUserForEmployeeNumber"],
    ["UpdateIncidentForSlaExitDate"],
    ["UpdateChangeRequestForFix"],
    ["UpdateCaseForFix"],
    ["UpdateCaseFinal"],
    ["UpdateProblemFinal"],
    ["UpdateChangeRequestFinal"],
    ["UpdateServiceRequestFinal"],
    ["UpdateAdminUserFinal"],
    ["UpdateUserForDept"],
    ["UpdateUserForFedID"],
    ["UpdateUserForFedIDEmailUN"],
    ["Service"],
    ["ServiceOffering"],
    ["Problem"],
    ["ChangeRequest"],
    ["ParentIncident"],
    ["ChildIncident"],
    ["InsertCase"],
    ["ServiceRequest"],
    ["SvcCatalogReqExtdAttrVal"],
    ["EmailMessage"],
    ["EmailMessageSR"],
    ["ArchivedEmailMessage"],
    ["ArchivedEmailMessageSR"],
    ["RelatedProblemIncident"],
    ["RelatedCRIncidentByCause"],
    ["RelatedCRIncidentByRFC"],
    ["ChatterNotes"],
    ["ChatterNotesSR"],
    ["ApprovalHistoryCR"],
    ["ApprovalHistorySR"],
]

# Priority
ATTACHMENT_MAPPING_PRIORITY = [
    ["AttachmentIncidents"],
    ["AttachmentProblem"],
    ["AttachmentChangeRequest"],
    ["AttachmentServiceRequest"],
]

# JSM Mapping Priority
JSM_MAPPING_PRIORITY = [
    ["JSMUser"],
    ["JSMOrganization"],
    ["JSMIncident"],
    ["JSMServiceRequest"],
    ["JSMProblem"],
    ["JSMChange"],
    ["JSMComment"],
    ["JSMAttachment"],
]

MAX_FILE_SIZE = 150 * 1024 * 1024

DISABLE_LOAD = False

FILTER_BY_DATE = ""
