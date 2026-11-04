"""Explicit blocker classifications; labels describe a cause, never authority."""

BLOCK_CAUSES = frozenset({"technical", "user_decision", "permission_required", "external_dependency", "coordination_required"})
LEGACY_BLOCK_CAUSE = "unclassified"


class BlockContractError(ValueError):
    code = "invalid_block_cause"

    def __init__(self):
        super().__init__(self.code)


def normalize_block_cause(value=None):
    """Keep legacy missing/null causes unknown; never infer them from prose.

    A technical label does not override a human instruction or permit an action.
    Owner, permissions and repeated-failure handling remain host decisions.
    """
    if value is None:
        return LEGACY_BLOCK_CAUSE
    if not isinstance(value, str) or value not in BLOCK_CAUSES:
        raise BlockContractError()
    return value
