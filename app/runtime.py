"""Process-wide singletons (shared services), set once at startup.

Tools read these instead of taking them as model-visible arguments - the model
never sees the PMS client, the cache or credentials.
"""
from __future__ import annotations

CLINIC: dict | None = None          # loaded clinic config (per-tenant data)
CACHE = None                        # ReferenceCache
PMS = None                          # PMSClient
