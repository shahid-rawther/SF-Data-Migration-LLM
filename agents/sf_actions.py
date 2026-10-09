"""The Salesforce changes a readiness suggestion may make, and nothing else.

Mapping changes go through :mod:`agents.resolutions`. Some gaps cannot be closed
by a mapping at all — a required lookup onto an object nothing migrates needs a
record to point at, and a source column with no home needs a field — so the
readiness agent may also propose one of two writes against the org itself:

* ``create_record`` — one record in an object the API can create, with every
  field Salesforce requires supplied. A later resolution in the same
  suggestion can point at it as ``{{action:N}}``.
* ``create_custom_field`` — a custom field through the Tooling API.

Both are validated against the live describe before a person sees them, and
again at apply time. Nothing runs until that person clicks Apply. A created
record is journalled and can be deleted to revert; a created field is
journalled but not deleted automatically, because dropping a field destroys
whatever has been written into it since.

Objects the API refuses to create (licences, profiles, the organisation) are
not part of this vocabulary by construction, and a few that it will create are
refused on purpose: a User consumes a paid licence seat, and a permission or
role record changes who can see what.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from common.logger import logger
from services.internal import sf_inventory

from agents.context import MigrationContext
from agents.resolutions import PLACEHOLDER, is_placeholder

ACTION_CREATE_RECORD = "create_record"
ACTION_CREATE_CUSTOM_FIELD = "create_custom_field"
ACTION_KINDS = frozenset({ACTION_CREATE_RECORD, ACTION_CREATE_CUSTOM_FIELD})

# Createable, but not something an agent should make on its own authority.
REFUSED_OBJECTS = {
    "User": "creating a User consumes a paid licence seat",
    "PermissionSet": "permission records change who can see and edit data",
    "PermissionSetAssignment": "permission records change who can see and edit data",
    "PermissionSetLicenseAssign": "licence assignments are an administrator decision",
    "UserRole": "roles change record visibility across the org",
    "Profile": "profiles cannot be created through the REST API",
}

FIELD_TYPES = {
    "Text", "Number", "TextArea", "LongTextArea", "Checkbox",
    "Date", "DateTime", "Url", "Email", "Phone",
}
_CUSTOM_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]*__c$")
_SALESFORCE_ID = re.compile(r"^[a-zA-Z0-9]{15}$|^[a-zA-Z0-9]{18}$")
_MAX_VALUE_LENGTH = 255


@dataclass
class ActionOutcome:
    accepted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def reject(self, payload: dict, reason: str) -> None:
        self.rejected.append({"action": payload, "reason": reason})
        logger.warning("[readiness] rejected action %s: %s", payload.get("kind"), reason)


def normalise(item: dict) -> dict:
    """The model-facing shape, reduced to the keys each kind uses."""
    kind = str(item.get("kind") or "")
    base = {
        "kind": kind,
        "object": str(item.get("object") or ""),
        "rationale": str(item.get("rationale") or ""),
    }
    if kind == ACTION_CREATE_RECORD:
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        base["values"] = {str(k): v for k, v in values.items()}
    elif kind == ACTION_CREATE_CUSTOM_FIELD:
        base.update(
            {
                "field_api_name": str(item.get("field_api_name") or ""),
                "field_label": str(item.get("field_label") or ""),
                "field_type": str(item.get("field_type") or "Text"),
                "length": int(item.get("length") or 255),
                "description": str(item.get("description") or ""),
            }
        )
    return base


def validate(context: MigrationContext, raw_actions: list) -> ActionOutcome:
    """Check each action against the org, in order.

    Order matters because a record may point at one created earlier in the
    same list, and never at one created later.
    """
    outcome = ActionOutcome()
    catalog = sf_inventory.object_catalog(context.destination_credentials)
    for index, item in enumerate(raw_actions or []):
        if not isinstance(item, dict):
            continue
        action = normalise(item)
        error = _validate_one(context, catalog, action, index, outcome.accepted)
        if error:
            outcome.reject(action, error)
            # Later actions may refer to this one by position, so a gap would
            # silently shift every placeholder after it.
            for rest in raw_actions[index + 1:]:
                if isinstance(rest, dict):
                    outcome.reject(normalise(rest), "an earlier action in this suggestion was rejected")
            break
        outcome.accepted.append(action)
    return outcome


def _validate_one(
    context: MigrationContext,
    catalog: dict[str, dict],
    action: dict,
    index: int,
    earlier: list[dict],
) -> str:
    kind = action["kind"]
    obj = action["object"]
    if kind not in ACTION_KINDS:
        return f"Unsupported action '{kind}'"
    if not action["rationale"]:
        return "A rationale is required for every action"
    if not obj:
        return "An action needs the object it applies to"
    if catalog and obj not in catalog:
        return f"{obj} is not an object the integration user can see"
    if obj in REFUSED_OBJECTS:
        return f"{obj} is not created automatically: {REFUSED_OBJECTS[obj]}"

    if kind == ACTION_CREATE_CUSTOM_FIELD:
        return _validate_custom_field(context, action)

    info = catalog.get(obj) or {}
    if catalog and not info.get("createable"):
        return f"{obj} cannot be created through the API; point at an existing record instead"
    fields = {entry["name"]: entry for entry in context.destination_fields(obj)}
    if not fields:
        return f"{obj} could not be described, so the record cannot be checked"
    values = action["values"]
    if not values:
        return "create_record needs field values"
    for name, value in values.items():
        meta = fields.get(name)
        if not meta:
            return f"'{name}' is not a field on {obj}"
        if not meta.get("createable"):
            return f"'{name}' cannot be set when creating {obj}"
        text = str(value if value is not None else "")
        if meta.get("type") == "reference":
            match = PLACEHOLDER.match(text.strip())
            if match:
                ref = int(match.group(1))
                if ref >= index:
                    return f"'{name}' refers to action {ref}, which is not created before this one"
                if earlier[ref]["kind"] != ACTION_CREATE_RECORD or earlier[ref]["object"] not in (
                    meta.get("reference_to") or []
                ):
                    return f"'{name}' cannot point at action {ref}'s {earlier[ref]['object']}"
            elif not _SALESFORCE_ID.match(text):
                return f"'{name}' is a lookup and needs a record id, not '{text[:40]}'"
        elif len(text) > max(int(meta.get("length") or 0), _MAX_VALUE_LENGTH):
            return f"'{name}' is longer than {obj} allows"
        allowed = meta.get("picklist_values") or []
        if allowed and meta.get("picklist_restricted") and text not in allowed:
            return f"'{text}' is not an allowed value for {obj}.{name}"
    missing = [
        name
        for name, meta in fields.items()
        if meta.get("required") and meta.get("createable") and name not in values
    ]
    if missing:
        return f"{obj} requires {', '.join(sorted(missing))}"
    return ""


def _validate_custom_field(context: MigrationContext, action: dict) -> str:
    name = action["field_api_name"]
    if not _CUSTOM_FIELD.match(name):
        return f"'{name}' is not a valid custom field name; it must end with __c"
    if action["field_type"] not in FIELD_TYPES:
        return f"'{action['field_type']}' is not a supported field type"
    if not action["field_label"]:
        return "A custom field needs a label"
    fields = context.destination_fields(action["object"])
    if not fields:
        return f"{action['object']} could not be described"
    if any(entry["name"].lower() == name.lower() for entry in fields):
        return f"{action['object']} already has a field named {name}"
    return ""


# ── placeholders ───────────────────────────────────────────────────────────────


def placeholders_in(resolutions: list[dict]) -> set[int]:
    """Which action positions a list of resolutions refers to."""
    found: set[int] = set()
    for item in resolutions or []:
        for value in _placeholder_slots(item):
            match = PLACEHOLDER.match(str(value).strip())
            if match:
                found.add(int(match.group(1)))
    return found


def _placeholder_slots(item: dict):
    yield item.get("constant_value", "")
    yield item.get("value_map_default", "")
    for value in (item.get("value_map") or {}).values():
        yield value


def substitute(resolutions: list[dict], created: dict[int, str]) -> list[dict]:
    """Replace ``{{action:N}}`` with the id of the record action N created."""

    def swap(value):
        match = PLACEHOLDER.match(str(value or "").strip())
        if not match:
            return value
        index = int(match.group(1))
        if index not in created:
            raise ValueError(f"No record was created for {{{{action:{index}}}}}")
        return created[index]

    result = []
    for item in resolutions or []:
        item = copy.deepcopy(item)
        if "constant_value" in item:
            item["constant_value"] = swap(item["constant_value"])
        if "value_map_default" in item:
            item["value_map_default"] = swap(item["value_map_default"])
        if isinstance(item.get("value_map"), dict):
            item["value_map"] = {k: swap(v) for k, v in item["value_map"].items()}
        result.append(item)
    return result


def _fill_values(values: dict, created: dict[int, str]) -> dict:
    filled = {}
    for key, value in values.items():
        match = PLACEHOLDER.match(str(value or "").strip())
        filled[key] = created[int(match.group(1))] if match else value
    return filled


# ── execution ──────────────────────────────────────────────────────────────────


def execute(actions: list[dict], credentials: dict) -> list[dict]:
    """Run approved actions in order and return what each one created.

    If one fails, records created earlier in the same list are deleted again,
    so a half-applied suggestion does not leave orphans behind. Fields are not
    rolled back; see the module docstring.
    """
    results: list[dict] = []
    created: dict[int, str] = {}
    try:
        for index, action in enumerate(actions):
            if action["kind"] == ACTION_CREATE_RECORD:
                values = _fill_values(action["values"], created)
                record_id = sf_inventory.insert_record(action["object"], values, credentials)
                created[index] = record_id
                results.append(
                    {
                        "index": index,
                        "kind": action["kind"],
                        "object": action["object"],
                        "record_id": record_id,
                        "values": values,
                        "revertible": True,
                    }
                )
            elif action["kind"] == ACTION_CREATE_CUSTOM_FIELD:
                _create_field(action, credentials)
                results.append(
                    {
                        "index": index,
                        "kind": action["kind"],
                        "object": action["object"],
                        "field": action["field_api_name"],
                        "values": {
                            "label": action["field_label"],
                            "type": action["field_type"],
                            "length": action["length"],
                        },
                        "revertible": False,
                    }
                )
    except Exception:
        for result in reversed(results):
            if result.get("record_id"):
                try:
                    sf_inventory.delete_record(result["object"], result["record_id"], credentials)
                except Exception:  # noqa: BLE001 — reported by the caller's error
                    logger.exception(
                        "[readiness] could not roll back %s %s", result["object"], result["record_id"]
                    )
        raise
    return results


def created_ids(results: list[dict]) -> dict[int, str]:
    return {r["index"]: r["record_id"] for r in results if r.get("record_id")}


def _create_field(action: dict, credentials: dict) -> None:
    from fastapi import HTTPException

    from integrations.salesforce.routes_custom_fields import (
        CreateCustomFieldRequest,
        create_custom_field,
    )

    try:
        response = create_custom_field(
            CreateCustomFieldRequest(
                destination_credentials=credentials,
                salesforce_object=action["object"],
                field_label=action["field_label"],
                field_api_name=action["field_api_name"],
                field_type=action["field_type"],
                length=action["length"],
                description=action.get("description", ""),
            )
        )
    except HTTPException as exc:
        raise sf_inventory.SalesforceWriteError(str(exc.detail)) from exc
    if not response.success:
        raise sf_inventory.SalesforceWriteError(response.message)


def uses_placeholders(resolutions: list[dict]) -> bool:
    return any(is_placeholder(v) for item in resolutions or [] for v in _placeholder_slots(item))
