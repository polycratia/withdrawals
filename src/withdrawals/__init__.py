"""Withdrawal pipeline: internal transfers off-chain, external ones on-chain."""

from withdrawals.approval import (
    ApprovalPolicy,
    ApprovalRule,
    NotApproved,
    SelfApproval,
    approve,
    ensure_approved,
)
from withdrawals.models import (
    Approval,
    InvalidTransition,
    WithdrawalError,
    WithdrawalRequest,
    WithdrawalState,
)
from withdrawals.routing import Custody, InMemoryCustody, Rail, Route, route
from withdrawals.submission import (
    IdempotencyConflict,
    SubmissionInFlight,
    Submissions,
)

__version__ = "0.1.0"

__all__ = [
    "Approval",
    "ApprovalPolicy",
    "ApprovalRule",
    "Custody",
    "IdempotencyConflict",
    "InMemoryCustody",
    "InvalidTransition",
    "NotApproved",
    "Rail",
    "Route",
    "SelfApproval",
    "SubmissionInFlight",
    "Submissions",
    "WithdrawalError",
    "WithdrawalRequest",
    "WithdrawalState",
    "__version__",
    "approve",
    "ensure_approved",
    "route",
]
