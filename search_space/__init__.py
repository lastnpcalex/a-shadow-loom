"""Policy-gated, local-first search across EXO conversation workspaces."""

from .index import SEARCH_POLICIES, SearchIndex, canonical_workspace, workspace_key
from .operator import SearchBackendUnavailable, SearchOperator, SearchPolicyError

__all__ = [
    "SEARCH_POLICIES",
    "SearchBackendUnavailable",
    "SearchIndex",
    "SearchOperator",
    "SearchPolicyError",
    "canonical_workspace",
    "workspace_key",
]
