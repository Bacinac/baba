"""Identity-level endpoints (split into domain submodules)."""

from baba_api.routes_identities import (  # noqa: F401  (route registration)
    _ai,
    _core,
    _immich,
    _merge,
    _opus,
    _reference_photos,
    _sightings,
)
from baba_api.routes_identities._ai import auto_describe_loop
from baba_api.routes_identities._base import identities_router

__all__ = ["auto_describe_loop", "identities_router"]
