"""Backward-compatibility shim.

The ServiceNow transformation functions moved to
``integrations/servicenow/transformations.py``. Existing imports such as
``import helpers.transformation_helper as transformation_helper`` and
``from helpers.transformation_helper import look_up_incident_sys_id`` keep
working via the module swap below (which also preserves ``getattr`` lookups
used by the transformation service to resolve functions by name).
"""
import sys as _sys

from integrations.servicenow import transformations as _impl

_sys.modules[__name__] = _impl
