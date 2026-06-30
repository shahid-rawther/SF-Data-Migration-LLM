"""Backward-compatibility shim.

The Salesforce attachment-loading logic moved to
``integrations/salesforce/loading.py``. Existing imports such as
``from services.internal import loading_service`` keep working via the module
swap below.
"""
import sys as _sys

from integrations.salesforce import loading as _impl

_sys.modules[__name__] = _impl
