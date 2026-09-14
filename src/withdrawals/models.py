"""The withdrawal request and the states it moves through.

A request travels requested -> approved -> sending -> sent -> confirmed, and
leaves the pipeline through rejected or failed. States are plain data: every
transition returns a new request and the caller decides where to store it.
Replaying a transition that already happened returns the same value, so a
callback delivered twice changes nothing.

Approvals travel with the request: each one names who gave it, when they gave
it and which policy rule was in force at the time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

__all__ = [
    "Approval",
    "InvalidTransition",
    "WithdrawalError",
    "WithdrawalRequest",
    "WithdrawalState",
]


class WithdrawalError(Exception):
    """Base class for the errors raised by this package."""


class InvalidTransition(WithdrawalError):
    """A transition the state machine does not allow was attempted."""


class WithdrawalState(str, Enum):
    """The states a withdrawal request can be in."""

    REQUESTED = "requested"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value

    @property
    def is_terminal(self) -> bool:
        """True when no transition leads out of this state."""
        return not _TRANSITIONS[self]


_TRANSITIONS: dict[WithdrawalState, frozenset[WithdrawalState]] = {
    WithdrawalState.REQUESTED: frozenset(
        {WithdrawalState.APPROVED, WithdrawalState.REJECTED}
    ),
    WithdrawalState.APPROVED: frozenset(
        {
            WithdrawalState.SENDING,
            WithdrawalState.REJECTED,
            WithdrawalState.FAILED,
        }
    ),
    WithdrawalState.SENDING: frozenset(
        {WithdrawalState.SENT, WithdrawalState.FAILED}
    ),
    WithdrawalState.SENT: frozenset(
        {WithdrawalState.CONFIRMED, WithdrawalState.FAILED}
    ),
    WithdrawalState.CONFIRMED: frozenset(),
    WithdrawalState.REJECTED: frozenset(),
    WithdrawalState.FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Approval:
    """One person letting a withdrawal through, under one policy rule."""

    approver: str
    at: datetime
    policy: str

    def __post_init__(self) -> None:
        approver = _require_text(self.approver, "approver").strip()
        policy = _require_text(self.policy, "policy").strip()
        if not isinstance(self.at, datetime):
            raise TypeError("at must be a datetime")
        if self.at.tzinfo is None or self.at.tzinfo.utcoffset(self.at) is None:
            raise ValueError("at must be timezone-aware")
        object.__setattr__(self, "approver", approver)
        object.__setattr__(self, "policy", policy)


@dataclass(frozen=True, slots=True)
class WithdrawalRequest:
    """A single withdrawal as it moves through the pipeline.

    The request holds no storage and no clock: it is a value that a caller
    reads, transitions and writes back.
    """

    id: str
    amount: Decimal
    currency: str
    destination: str
    state: WithdrawalState = WithdrawalState.REQUESTED
    reference: str | None = None
    reason: str | None = None
    requested_by: str | None = None
    approvals: tuple[Approval, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("id must be a non-empty string")
        if isinstance(self.amount, float):
            raise TypeError("amount must be a Decimal, not a float")
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal")
        if not self.amount.is_finite() or self.amount <= 0:
            raise ValueError("amount must be a positive finite Decimal")
        if (
            not isinstance(self.currency, str)
            or not self.currency.isalnum()
            or not 2 <= len(self.currency) <= 12
        ):
            raise ValueError("currency must be an alphanumeric ticker")
        if not isinstance(self.destination, str) or not self.destination.strip():
            raise ValueError("destination must be a non-empty string")
        if self.requested_by is not None:
            object.__setattr__(
                self,
                "requested_by",
                _require_text(self.requested_by, "requested_by").strip(),
            )
        object.__setattr__(self, "approvals", _approvals(self.approvals))
        object.__setattr__(self, "currency", self.currency.upper())
        object.__setattr__(self, "state", WithdrawalState(self.state))

    @property
    def is_terminal(self) -> bool:
        """True once the request has confirmed, been rejected or failed."""
        return self.state.is_terminal

    @property
    def approvers(self) -> tuple[str, ...]:
        """The people who have approved so far, in the order they did."""
        return tuple(approval.approver for approval in self.approvals)

    def record_approval(self, approval: Approval) -> WithdrawalRequest:
        """Add one approval; the same approver recorded twice counts once."""
        if not isinstance(approval, Approval):
            raise TypeError("an approval must be an Approval")
        if approval.approver in self.approvers:
            return self
        if self.state is not WithdrawalState.REQUESTED:
            raise InvalidTransition(
                f"withdrawal {self.id} is {self.state} "
                f"and takes no further approvals"
            )
        return replace(self, approvals=self.approvals + (approval,))

    def approve(self) -> WithdrawalRequest:
        if self.state is WithdrawalState.APPROVED:
            return self
        return self._move(WithdrawalState.APPROVED)

    def reject(self, reason: str) -> WithdrawalRequest:
        """Turn the request down; only possible while the money has not moved."""
        reason = _require_text(reason, "reason")
        if self.state is WithdrawalState.REJECTED:
            return self._replay(reason=reason)
        return self._move(WithdrawalState.REJECTED, reason=reason)

    def start_sending(self) -> WithdrawalRequest:
        if self.state is WithdrawalState.SENDING:
            return self
        return self._move(WithdrawalState.SENDING)

    def mark_sent(self, reference: str) -> WithdrawalRequest:
        """Record the transfer or transaction reference handed back by the rail."""
        reference = _require_text(reference, "reference")
        if self.state is WithdrawalState.SENT:
            return self._replay(reference=reference)
        return self._move(WithdrawalState.SENT, reference=reference)

    def confirm(self) -> WithdrawalRequest:
        if self.state is WithdrawalState.CONFIRMED:
            return self
        return self._move(WithdrawalState.CONFIRMED)

    def fail(self, reason: str) -> WithdrawalRequest:
        reason = _require_text(reason, "reason")
        if self.state is WithdrawalState.FAILED:
            return self._replay(reason=reason)
        return self._move(WithdrawalState.FAILED, reason=reason)

    def _move(self, state: WithdrawalState, **changes: Any) -> WithdrawalRequest:
        if state not in _TRANSITIONS[self.state]:
            raise InvalidTransition(
                f"withdrawal {self.id} cannot go from {self.state} to {state}"
            )
        return replace(self, state=state, **changes)

    def _replay(self, **expected: Any) -> WithdrawalRequest:
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise InvalidTransition(
                    f"withdrawal {self.id} is already {self.state} "
                    f"with a different {name}"
                )
        return self


def _approvals(values: tuple[Approval, ...]) -> tuple[Approval, ...]:
    approvals = tuple(values)
    seen: set[str] = set()
    for approval in approvals:
        if not isinstance(approval, Approval):
            raise TypeError("approvals must be Approval values")
        if approval.approver in seen:
            raise ValueError("the same approver cannot appear twice")
        seen.add(approval.approver)
    return approvals


def _require_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value
