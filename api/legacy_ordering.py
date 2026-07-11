"""Legacy dependency resolution for the shipped ServiceNow migration pack.

These helpers are used by the CLI path (``app.py``) and the hardcoded JSON
mapping pack.  They are **not** used by the wizard runtime
(``migration_manager``), which relies on schema-based topological ordering
via ``compat_engine.resolve_dependency_order``.
"""
from __future__ import annotations

from common.logger import logger

# Map of ServiceNow tables to the migration object that must be extracted
# first to provide lookup data for transformations.
TABLE_DEPENDENCY_MAP: dict[str, str] = {
    "sys_user": "Account",
    "sys_user_group": "Group",
    "cmn_location": "ParentLocation",
    "cmn_department": "Department",
    "core_company": "Company",
}

# Transformation function names that imply a dependency on a specific table.
FUNCTION_DEPENDENCY_MAP: dict[str, str] = {
    "look_up_user_sys_id": "sys_user",
    "look_up_user_user_name": "sys_user",
    "look_up_assignement_group": "sys_user_group",
    "look_up_location_sys_id": "cmn_location",
    "look_up_company_sys_id": "core_company",
}


def resolve_legacy_dependencies(
    selected_objects: list[str],
    all_priority_groups: list[list[str]],
) -> list[str]:
    """Ensure all dependency objects are included and ordered correctly.

    Scans each selected object's mapping config (from DB first, JSON fallback)
    for transformation functions that require lookup tables from other objects.
    If those prerequisite objects are not already selected, they are
    automatically added.

    This is intended for the shipped ServiceNow mapping pack only.
    """
    from services.internal.mappings_service import get_mapping_config

    needed: set[str] = set(selected_objects)

    for obj_name in list(selected_objects):
        mapping = get_mapping_config(obj_name)
        if not mapping:
            continue
        for field_def in mapping.get("fields", []):
            func = field_def.get("transformation_function", "")
            if func in FUNCTION_DEPENDENCY_MAP:
                table = FUNCTION_DEPENDENCY_MAP[func]
                dep_object = TABLE_DEPENDENCY_MAP.get(table)
                if dep_object and dep_object not in needed:
                    needed.add(dep_object)
                    logger.info(
                        "Auto-including '%s' as dependency for '%s' (requires %s)",
                        dep_object, obj_name, table,
                    )

    all_flat = [name for group in all_priority_groups for name in group]
    ordered = [name for name in all_flat if name in needed]
    for name in needed:
        if name not in ordered:
            ordered.append(name)
    return ordered
