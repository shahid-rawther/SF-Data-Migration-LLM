"""Compatibility router — pre-migration compatibility checking."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from api.compat_engine import check_compatibility, CompatibilityReport

router = APIRouter(prefix="/compat", tags=["compatibility"])


class CompatCheckRequest(BaseModel):
    source_type: str
    source_credentials: dict = {}
    destination_credentials: dict = {}
    selected_objects: list[str] = []


class FieldResultOut(BaseModel):
    source_field: str
    destination_field: str
    status: str
    source_type: str
    destination_type: str
    suggestion: str


class ObjectCompatibilityOut(BaseModel):
    object_name: str
    source_label: str
    salesforce_object: str
    total_fields: int
    mapped_fields: int
    missing_fields: int
    type_mismatches: int
    compatibility_pct: float
    fields: list[FieldResultOut]
    blockers: list[str]


class CompatReportOut(BaseModel):
    source_type: str
    overall_pct: float
    objects: list[ObjectCompatibilityOut]
    summary: str


@router.post("/check", response_model=CompatReportOut)
def check_compat(body: CompatCheckRequest) -> CompatReportOut:
    """Run a pre-migration compatibility check.

    Returns a structured report showing which source fields map to
    Salesforce fields, which are missing, and actionable suggestions.
    """
    report: CompatibilityReport = check_compatibility(
        source_type=body.source_type,
        source_credentials=body.source_credentials,
        destination_credentials=body.destination_credentials,
        selected_objects=body.selected_objects,
    )
    return CompatReportOut(
        source_type=report.source_type,
        overall_pct=report.overall_pct,
        summary=report.summary,
        objects=[
            ObjectCompatibilityOut(
                object_name=obj.object_name,
                source_label=obj.source_label,
                salesforce_object=obj.salesforce_object,
                total_fields=obj.total_fields,
                mapped_fields=obj.mapped_fields,
                missing_fields=obj.missing_fields,
                type_mismatches=obj.type_mismatches,
                compatibility_pct=obj.compatibility_pct,
                fields=[
                    FieldResultOut(
                        source_field=f.source_field,
                        destination_field=f.destination_field,
                        status=f.status,
                        source_type=f.source_type,
                        destination_type=f.destination_type,
                        suggestion=f.suggestion,
                    )
                    for f in obj.fields
                ],
                blockers=obj.blockers,
            )
            for obj in report.objects
        ],
    )
