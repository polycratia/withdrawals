"""The withdrawal request and the states it moves through.

A request travels requested -> approved -> sending -> sent -> confirmed, and
leaves the pipeline through rejected or failed. States are plain data: every
transition returns a new request and the caller decides where to store it.
Replaying a transition that already happened returns the same value, so a
callback delivered twice changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Any

__all__ = [
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
        object.__setattr__(self, "currency", self.currency.upper())
        object.__setattr__(self, "state", WithdrawalState(self.state))

    @property
    def is_terminal(self) -> bool:
        """True once the request has confirmed, been rejected or failed."""
        return self.state.is_terminal

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


def _require_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value
