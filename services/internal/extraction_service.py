"""Backward-compatibility facade for extraction.

Extraction logic now lives with each integration:
- ServiceNow:  ``integrations/servicenow/extraction.py``
- Salesforce:  ``integrations/salesforce/extraction.py``

This module re-exports the previous public functions so existing call sites
(``from services.internal import extraction_service`` then
``extraction_service.extract_servicenow_tables(...)`` etc.) keep working.
"""
from integrations.servicenow.extraction import (  # noqa: F401
    extract_servicenow_tables,
    extract_servicenow_table,
    extract_single_batch,
    extract_servicenow_files,
    download_attachment_batch,
    _download_single_attachment,
)
from integrations.salesforce.extraction import (  # noqa: F401
    extract_salesforce_objects,
    extract_salesforce_table,
)
