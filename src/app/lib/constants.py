from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from app.lib.service import Service
"""Name of the favicon file in the static directory"""
DB_SESSION_DEPENDENCY_KEY = "db_session"
"""The name of the key used for dependency injection of the database
session."""
USER_DEPENDENCY_KEY = "current_user"
"""The name of the key used for dependency injection of the database
session."""
DTO_INFO_KEY = "info"
"""The name of the key used for storing DTO information."""
DEFAULT_PAGINATION_SIZE = 20
"""Default page size to use."""
SERVICE_OBJECT_IDENTITY_MAP: MutableMapping[str, type[Service[Any]]] = {}
"""Used by the worker to lookup methods for service object callbacks."""
