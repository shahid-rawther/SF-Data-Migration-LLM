"""Salesforce-specific FastAPI routers.

Aggregates the destination (credential validation / diagnostics) and custom
field routers so the plugin can expose them to the app via ``get_routers()``.
"""
from fastapi import APIRouter

from integrations.salesforce.routes_destinations import router as destinations_router
from integrations.salesforce.routes_custom_fields import router as custom_fields_router

routers: list[APIRouter] = [destinations_router, custom_fields_router]
