"""What happens when the money does not leave.

A broadcast the rail refuses releases the hold in the same call that fails, so
the funds are back before the caller sees the error. An outcome nobody knows
keeps its hold instead: releasing money that may still be moving is worse than
holding it one reconciliation longer.

A transaction that does not confirm is not left alone either. Past the bump
deadline it is replaced by one the chain will take; past the cancel deadline,
or once it has been bumped as often as the policy allows, it is replaced by one
that returns the funds. The decision is a value the caller keeps, and it is
logged when it is taken.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from withdrawals.models import (
    InvalidTransition,
    WithdrawalError,
    WithdrawalRequest,
    WithdrawalState,
)

__all__ = [
    "Action",
    "BroadcastRejected",
    "Hold",
    "HoldMismatch",
    "Holds",
    "InMemoryHolds",
    "Recovery",
    "StuckPolicy",
    "broadcast",
    "bump",
    "cancel",
    "return_funds",
    "review",
]

logger = logging.getLogger(__name__)


class BroadcastRejected(WithdrawalError):
    """The rail refused the transaction and nothing left the account."""


class HoldMismatch(WithdrawalError):
    """A hold was placed again for the same withdrawal with other numbers."""


@dataclass(frozen=True, slots=True)
class Hold:
    """The funds reserved for one withdrawal until it settles or fails."""

    withdrawal_id: str
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.withdrawal_id, str) or not self.withdrawal_id.strip():
            raise ValueError("withdrawal_id must be a non-empty string")
        if isinstance(self.amount, float):
            raise TypeError("amount must be a Decimal, not a float")
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal")
        if not self.amount.is_finite() or self.amount <= 0:
            raise ValueError("amount must be a positive finite Decimal")
        if not isinstance(self.currency, str) or not self.currency.isalnum():
            raise ValueError("currency must be an alphanumeric ticker")
        object.__setattr__(self, "currency", self.currency.upper())

    @classmethod
    def on(cls, request: WithdrawalRequest) -> Hold:
        """The hold a request needs while it is on its way."""
        return cls(
            withdrawal_id=request.id,
            amount=request.amount,
            currency=request.currency,
        )


@runtime_checkable
class Holds(Protocol):
    """Where the reserved funds sit until a withdrawal settles or fails."""

    def release(self, withdrawal_id: str) -> bool:
        """Give the funds back, and say whether any were still held."""


class InMemoryHolds:
    """Holds kept in this process, behind the same lock for every caller."""

    __slots__ = ("_holds", "_lock")

    def __init__(self, holds: Iterable[Hold] = ()) -> None:
        self._lock = threading.Lock()
        self._holds: dict[str, Hold] = {}
        for hold in holds:
            self.place(hold)

    def place(self, hold: Hold) -> Hold:
        """Reserve the funds for one withdrawal; placing it twice is free."""
        if not isinstance(hold, Hold):
            raise TypeError("a hold must be a Hold")
        with self._lock:
            kept = self._holds.get(hold.withdrawal_id)
            if kept is not None and kept != hold:
                raise HoldMismatch(
                    f"withdrawal {hold.withdrawal_id} already holds "
                    f"{kept.amount} {kept.currency}"
                )
            self._holds[hold.withdrawal_id] = hold
        return hold

    def release(self, withdrawal_id: str) -> bool:
        """Give the funds back; False when nothing was held."""
        with self._lock:
            return self._holds.pop(withdrawal_id, None) is not None

    def held(self, withdrawal_id: str) -> Hold | None:
        """The hold standing for `withdrawal_id`, or None."""
        with self._lock:
            return self._holds.get(withdrawal_id)


class Action(str, Enum):
    """What to do about a transaction that has not confirmed yet."""

    WAIT = "wait"
    BUMP = "bump"
    CANCEL = "cancel"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Recovery:
    """What a stuck transaction needs, with the reason behind it."""

    action: Action
    reason: str
    waited: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.action, Action):
            raise TypeError("action must be an Action")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        if not isinstance(self.waited, timedelta):
            raise TypeError("waited must be a timedelta")

    @property
    def is_wait(self) -> bool:
        """True while the chain still deserves the benefit of the doubt."""
        return self.action is Action.WAIT


@dataclass(frozen=True, slots=True)
class StuckPolicy:
    """How long a transaction may sit unconfirmed, and how often it is bumped."""

    bump_after: timedelta
    cancel_after: timedelta
    bumps: int = 3

    def __post_init__(self) -> None:
        for name in ("bump_after", "cancel_after"):
            value = getattr(self, name)
            if not isinstance(value, timedelta):
                raise TypeError(f"{name} must be a timedelta")
            if value <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if self.cancel_after < self.bump_after:
            raise ValueError("cancel_after cannot come before bump_after")
        if (
            not isinstance(self.bumps, int)
            or isinstance(self.bumps, bool)
            or self.bumps < 1
        ):
            raise ValueError("a policy must allow at least one bump")


def return_funds(
    request: WithdrawalRequest, reason: str, holds: Holds
) -> WithdrawalRequest:
    """Fail `request` and give back whatever was held for it."""
    failed = request.fail(reason)
    if holds.release(request.id):
        logger.warning(
            "withdrawal %s failed (%s); its hold was released", request.id, reason
        )
    else:
        logger.info(
            "withdrawal %s failed (%s); nothing was held for it", request.id, reason
        )
    return failed


def broadcast(
    request: WithdrawalRequest,
    send: Callable[[WithdrawalRequest], WithdrawalRequest],
    holds: Holds,
) -> WithdrawalRequest:
    """Send `request`, and give its hold back when the rail refuses it.

    A `send` that raises `BroadcastRejected` says nothing left the account: the
    request fails and the hold is released. Any other exception leaves the
    outcome unknown, so the hold stays and the error travels on.
    """
    pending = request.start_sending()
    try:
        outcome = send(pending)
    except BroadcastRejected as refusal:
        reason = str(refusal) or "the rail refused the broadcast"
        return return_funds(pending, reason, holds)
    except BaseException:
        logger.warning(
            "withdrawal %s came back with an unknown outcome; its hold stays "
            "until the rail has been reconciled",
            pending.id,
        )
        raise
    if not isinstance(outcome, WithdrawalRequest):
        raise TypeError("a send must return a WithdrawalRequest")
    return outcome


def review(
    request: WithdrawalRequest,
    *,
    sent_at: datetime,
    policy: StuckPolicy,
    now: datetime | None = None,
) -> Recovery:
    """Decide what a transaction that has not confirmed yet needs."""
    if request.state is not WithdrawalState.SENT:
        raise InvalidTransition(
            f"withdrawal {request.id} is {request.state}, "
            f"not a transaction waiting for the chain"
        )
    moment = datetime.now(timezone.utc) if now is None else now
    waited = _aware(moment, "now") - _aware(sent_at, "sent_at")
    bumps = len(request.replaced)
    if waited >= policy.cancel_after:
        decision = Recovery(
            Action.CANCEL,
            f"unconfirmed for {waited}, past the {policy.cancel_after} it is given",
            waited,
        )
    elif waited < policy.bump_after:
        decision = Recovery(
            Action.WAIT,
            f"unconfirmed for {waited}, bumped at {policy.bump_after}",
            waited,
        )
    elif bumps >= policy.bumps:
        decision = Recovery(
            Action.CANCEL,
            f"bumped {bumps} times already, the policy allows {policy.bumps}",
            waited,
        )
    else:
        decision = Recovery(
            Action.BUMP,
            f"unconfirmed for {waited}, past the {policy.bump_after} it is given",
            waited,
        )
    log = logger.info if decision.is_wait else logger.warning
    log("withdrawal %s: %s (%s)", request.id, decision.action, decision.reason)
    return decision


def bump(request: WithdrawalRequest, reference: str) -> WithdrawalRequest:
    """Put `reference` in front of the transaction that is not moving."""
    bumped = request.bump(reference)
    if bumped is request:
        logger.info("withdrawal %s already rides on %s", request.id, reference)
        return request
    logger.warning(
        "withdrawal %s bumped from %s to %s",
        bumped.id,
        bumped.replaced[-1],
        bumped.reference,
    )
    return bumped


def cancel(
    request: WithdrawalRequest,
    reference: str,
    holds: Holds,
    *,
    reason: str = "the transaction was cancelled before it confirmed",
) -> WithdrawalRequest:
    """Replace a stuck transaction with one that returns the funds."""
    replacement = request if request.reference == reference else request.bump(reference)
    return return_funds(replacement, reason, holds)


def _aware(moment: datetime, name: str) -> datetime:
    if not isinstance(moment, datetime):
        raise TypeError(f"{name} must be a datetime")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return moment
