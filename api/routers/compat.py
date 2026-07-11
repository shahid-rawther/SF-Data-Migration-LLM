"""Compatibility router — pre-migration compatibility checking."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from api.compat_engine import check_compatibility, get_type_coercion, CompatibilityReport

router = APIRouter(prefix="/compat", tags=["compatibility"])


class CompatCheckRequest(BaseModel):
    source_type: str
    source_credentials: dict = {}
    destination_credentials: dict = {}
    selected_objects: list[str] = []


class TypeCheckRequest(BaseModel):
    source_type: str
    destination_type: str


class TypeCheckResponse(BaseModel):
    compatible: bool
    coercible: bool = False
    transform: str = ""
    params: dict = {}
    warning: str = ""


class FieldResultOut(BaseModel):
    source_field: str
    destination_field: str
    status: str
    source_type: str
    destination_type: str
    suggestion: str
    suggested_custom_field: str = ""
    suggested_field_type: str = "Text"


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


class DependencyEdgeOut(BaseModel):
    from_mapping: str
    to_mapping: str
    field: str
    required: bool


class DependencyWarningOut(BaseModel):
    mapping: str
    field: str
    references: str
    reason: str


class BlockingRequiredFieldOut(BaseModel):
    mapping: str
    field: str
    reason: str


class PicklistWarningOut(BaseModel):
    mapping: str
    field: str
    reason: str


class CompatReportOut(BaseModel):
    source_type: str
    overall_pct: float
    objects: list[ObjectCompatibilityOut]
    summary: str
    dependency_order: list[str] = []
    dependency_edges: list[DependencyEdgeOut] = []
    dependency_warnings: list[DependencyWarningOut] = []
    dependency_missing_parents: list[DependencyWarningOut] = []
    dependency_has_cycle: bool = False
    blocking_required_fields: list[BlockingRequiredFieldOut] = []
    picklist_mapping_warnings: list[PicklistWarningOut] = []


@router.post("/check", response_model=CompatReportOut)
def check_compat(body: CompatCheckRequest) -> CompatReportOut:
    """Run a pre-migration compatibility check.

    Returns a structured report showing which source fields map to
    Salesforce fields, which are missing, and actionable suggestions.
    Also includes dependency analysis with execution order.
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
                        suggested_custom_field=f.suggested_custom_field,
                        suggested_field_type=f.suggested_field_type,
                    )
                    for f in obj.fields
                ],
                blockers=obj.blockers,
            )
            for obj in report.objects
        ],
        dependency_order=report.dependency_order,
        dependency_edges=[
            DependencyEdgeOut(
                from_mapping=e.from_mapping,
                to_mapping=e.to_mapping,
                field=e.field,
                required=e.required,
            )
            for e in report.dependency_edges
        ],
        dependency_warnings=[
            DependencyWarningOut(
                mapping=w.mapping,
                field=w.field,
                references=w.references,
                reason=w.reason,
            )
            for w in report.dependency_warnings
        ],
        dependency_missing_parents=[
            DependencyWarningOut(
                mapping=mp.mapping,
                field=mp.field,
                references=mp.references,
                reason=mp.reason,
            )
            for mp in report.dependency_missing_parents
        ],
        dependency_has_cycle=report.dependency_has_cycle,
        blocking_required_fields=[
            BlockingRequiredFieldOut(
                mapping=b.get("mapping", ""),
                field=b.get("field", ""),
                reason=b.get("reason", ""),
            )
            for b in report.blocking_required_fields
        ],
        picklist_mapping_warnings=[
            PicklistWarningOut(
                mapping=p.get("mapping", ""),
                field=p.get("field", ""),
                reason=p.get("reason", ""),
            )
            for p in report.picklist_mapping_warnings
        ],
    )


@router.post("/type-check", response_model=TypeCheckResponse)
def type_check(body: TypeCheckRequest) -> TypeCheckResponse:
    """Check whether two field types are compatible and suggest coercion."""
    result = get_type_coercion(body.source_type, body.destination_type)
    return TypeCheckResponse(
        compatible=result.get("compatible", False),
        coercible=result.get("coercible", False),
        transform=result.get("transform", ""),
        params=result.get("params", {}),
        warning=result.get("warning", ""),
    )
