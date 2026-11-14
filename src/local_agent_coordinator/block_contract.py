"""Explicit blocker classifications; labels describe a cause, never authority."""

BLOCK_CAUSES = frozenset({"technical", "user_decision", "permission_required", "external_dependency", "coordination_required"})
LEGACY_BLOCK_CAUSE = "unclassified"
# Only these two ask the user: a choice that is theirs, or a permission only
# they can grant. Every other block a partner raises, a missing cause included,
# goes back to the partners, who replan, hand the work to someone else or try
# another way. The host never turns those into a question for the user.
USER_BLOCK_CAUSES = frozenset({"user_decision", "permission_required"})
PARTNER_BLOCK_CAUSES = (BLOCK_CAUSES | {LEGACY_BLOCK_CAUSE}) - USER_BLOCK_CAUSES


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
