"""Salesforce workflow check.

Runs after mapping. It walks Object, Trigger, Entry Conditions, Decisions,
Actions, Approvals, Notifications, and Error Handling for each selected
mapping and reports whether the mapped data can follow that template.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from integrations.salesforce.workflow import check_mappings

router = APIRouter(prefix="/workflow", tags=["workflow"])


class WorkflowCheckRequest(BaseModel):
    mapping_names: list[str] = []
    destination_credentials: dict = {}
    records_by_mapping: dict = {}
    use_transformed_rows: bool = False


class FindingOut(BaseModel):
    stage: str
    status: str
    name: str
    detail: str


class StageOut(BaseModel):
    stage: str
    label: str
    status: str
    detail: str
    findings: list[FindingOut]


class ObjectWorkflowOut(BaseModel):
    mapping: str
    salesforce_object: str
    template_found: bool
    ok: bool
    failing_rows: int
    summary: str
    stages: list[StageOut]


class WorkflowReportOut(BaseModel):
    ok: bool
    summary: str
    objects: list[ObjectWorkflowOut]


@router.post("/check", response_model=WorkflowReportOut)
def check_workflow(body: WorkflowCheckRequest) -> dict:
    """Check mapped data against the Salesforce workflow template."""
    report = check_mappings(
        mapping_names=body.mapping_names,
        destination_credentials=body.destination_credentials,
        records_by_mapping=body.records_by_mapping or None,
        use_transformed_rows=body.use_transformed_rows,
    )
    return report.to_dict()
