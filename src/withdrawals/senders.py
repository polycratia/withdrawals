"""Moving the money: a credit inside custody, a transaction outside it.

Routing decides which rail a withdrawal travels; these are the two sends that
travel it. The internal one credits the account the decision names: the entry
is booked the moment the ledger takes it, so there is nothing to wait for and
the request comes back confirmed. The external one hands the transfer to the
chain, keeps the transaction it gives back and leaves the request waiting for
confirmations.

Both are plain callables of one request, which is what `broadcast` and
`Submissions.submit` take. A rail that refuses the transfer says so with
`BroadcastRejected`, so the hold goes back in the same call; and each send
keeps its own rail on the request, so neither can move money the other way.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import partial
from typing import Protocol, runtime_checkable

from withdrawals.models import Rail, WithdrawalRequest
from withdrawals.routing import Route
from withdrawals.submission import IdempotencyConflict

__all__ = [
    "Chain",
    "InMemoryLedger",
    "Ledger",
    "LedgerEntry",
    "Rails",
    "Sender",
    "send_external",
    "send_internal",
]

logger = logging.getLogger(__name__)

Sender = Callable[[WithdrawalRequest], WithdrawalRequest]


@runtime_checkable
class Ledger(Protocol):
    """Where a transfer that stays on our own books is booked."""

    def credit(
        self, account: str, amount: Decimal, currency: str, *, withdrawal_id: str
    ) -> str:
        """Credit `account` and return the entry that was booked."""


@runtime_checkable
class Chain(Protocol):
    """Where a transfer that leaves our books is broadcast."""

    def send(
        self, destination: str, amount: Decimal, currency: str, *, withdrawal_id: str
    ) -> str:
        """Broadcast the transfer and return the transaction that carries it."""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One credit booked for one withdrawal."""

    reference: str
    withdrawal_id: str
    account: str
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        for name in ("reference", "withdrawal_id", "account"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if isinstance(self.amount, float):
            raise TypeError("amount must be a Decimal, not a float")
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal")
        if not self.amount.is_finite() or self.amount <= 0:
            raise ValueError("amount must be a positive finite Decimal")
        if not isinstance(self.currency, str) or not self.currency.isalnum():
            raise ValueError("currency must be an alphanumeric ticker")
        object.__setattr__(self, "currency", self.currency.upper())


class InMemoryLedger:
    """A ledger kept in this process: one entry per withdrawal, booked once.

    Asking for the same credit again gives back the reference the first one
    got; asking for another credit under the same withdrawal is a conflict.
    """

    __slots__ = ("_entries", "_lock", "_prefix")

    def __init__(self, prefix: str = "ledger") -> None:
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("prefix must be a non-empty string")
        self._prefix = prefix.strip()
        self._lock = threading.Lock()
        self._entries: dict[str, LedgerEntry] = {}

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        """The entries booked so far, in the order they were booked."""
        with self._lock:
            return tuple(self._entries.values())

    def entry_for(self, withdrawal_id: str) -> LedgerEntry | None:
        """The entry booked for `withdrawal_id`, or None."""
        if not isinstance(withdrawal_id, str) or not withdrawal_id.strip():
            raise ValueError("withdrawal id must be a non-empty string")
        with self._lock:
            return self._entries.get(withdrawal_id.strip())

    def credit(
        self, account: str, amount: Decimal, currency: str, *, withdrawal_id: str
    ) -> str:
        """Book the credit, or give back the entry that already carries it."""
        with self._lock:
            entry = LedgerEntry(
                reference=f"{self._prefix}-{len(self._entries) + 1}",
                withdrawal_id=withdrawal_id,
                account=account,
                amount=amount,
                currency=currency,
            )
            booked = self._entries.get(entry.withdrawal_id)
            if booked is not None:
                if booked != replace(entry, reference=booked.reference):
                    raise IdempotencyConflict(
                        f"withdrawal {booked.withdrawal_id} is already credited "
                        f"as {booked.amount} {booked.currency} to {booked.account}"
                    )
                return booked.reference
            self._entries[entry.withdrawal_id] = entry
        logger.info(
            "withdrawal %s credited to %s as %s",
            entry.withdrawal_id,
            entry.account,
            entry.reference,
        )
        return entry.reference


def send_internal(
    request: WithdrawalRequest, decision: Route, ledger: Ledger
) -> WithdrawalRequest:
    """Credit the account `decision` names, and confirm: the entry is booked."""
    if decision.account is None:
        raise ValueError("an internal send needs the account its route names")
    pending = request.routed(Rail.INTERNAL).start_sending()
    reference = ledger.credit(
        decision.account,
        pending.amount,
        pending.currency,
        withdrawal_id=pending.id,
    )
    booked = pending.mark_sent(reference)
    logger.info(
        "withdrawal %s booked against %s as %s",
        booked.id,
        decision.account,
        booked.reference,
    )
    return booked.confirm()


def send_external(request: WithdrawalRequest, chain: Chain) -> WithdrawalRequest:
    """Hand the transfer to the chain and keep the transaction it gave back."""
    pending = request.routed(Rail.EXTERNAL).start_sending()
    reference = chain.send(
        pending.destination,
        pending.amount,
        pending.currency,
        withdrawal_id=pending.id,
    )
    sent = pending.mark_sent(reference)
    logger.info(
        "withdrawal %s broadcast to %s as %s",
        sent.id,
        sent.destination,
        sent.reference,
    )
    return sent


class Rails:
    """The ledger and the chain, behind the send each decision asks for."""

    __slots__ = ("_chain", "_ledger")

    def __init__(self, ledger: Ledger, chain: Chain) -> None:
        self._ledger = ledger
        self._chain = chain

    def sender(self, decision: Route) -> Sender:
        """The send that moves the money the way `decision` chose."""
        if not isinstance(decision, Route):
            raise TypeError("a send is chosen by a Route")
        if decision.is_internal:
            return partial(send_internal, decision=decision, ledger=self._ledger)
        return partial(send_external, chain=self._chain)
