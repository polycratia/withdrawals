"""Where a withdrawal goes: a ledger move inside custody, or a send on-chain.

A destination we custody never reaches a chain. Crediting it is a ledger move:
no fee, instant, nothing to confirm. Everything else is a transaction on the
rail the currency lives on. The decision is a value the caller keeps, and it is
logged when it is taken, so the two paths can always be told apart afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from withdrawals.models import WithdrawalRequest

__all__ = [
    "Custody",
    "InMemoryCustody",
    "Rail",
    "Route",
    "route",
]

logger = logging.getLogger(__name__)


class Rail(str, Enum):
    """The two ways a withdrawal can leave the account it came from."""

    INTERNAL = "internal"
    EXTERNAL = "external"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Route:
    """The routing decision for one withdrawal, with the reason behind it."""

    rail: Rail
    destination: str
    reason: str
    account: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.destination, str) or not self.destination.strip():
            raise ValueError("destination must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        if self.rail is Rail.INTERNAL:
            if not isinstance(self.account, str) or not self.account.strip():
                raise ValueError("an internal route must name the account it credits")
        elif self.account is not None:
            raise ValueError("an external route has no internal account")

    @property
    def is_internal(self) -> bool:
        """True when the transfer stays on our own books."""
        return self.rail is Rail.INTERNAL

    @property
    def needs_confirmations(self) -> bool:
        """True when the transfer only settles once the chain confirms it."""
        return self.rail is Rail.EXTERNAL


@runtime_checkable
class Custody(Protocol):
    """Answers whether a destination is an address we hold ourselves."""

    def account_for(self, destination: str) -> str | None:
        """Return the internal account behind `destination`, or None."""


class InMemoryCustody:
    """Custody backed by a mapping of deposit address to internal account."""

    __slots__ = ("_accounts",)

    def __init__(self, accounts: Mapping[str, str]) -> None:
        self._accounts: dict[str, str] = {}
        for destination, account in accounts.items():
            if not destination.strip() or not account.strip():
                raise ValueError("custody entries need an address and an account")
            self._accounts[_lookup_key(destination)] = account

    def account_for(self, destination: str) -> str | None:
        return self._accounts.get(_lookup_key(destination))


def route(request: WithdrawalRequest, custody: Custody) -> Route:
    """Decide which rail `request` travels on and log the decision."""
    account = custody.account_for(request.destination)
    if account is None:
        decision = Route(
            rail=Rail.EXTERNAL,
            destination=request.destination,
            reason="destination is not an address we custody",
        )
    else:
        decision = Route(
            rail=Rail.INTERNAL,
            destination=request.destination,
            reason=f"destination is custodied by account {account}",
            account=account,
        )
    logger.info(
        "withdrawal %s routed %s to %s: %s",
        request.id,
        decision.rail,
        decision.destination,
        decision.reason,
    )
    return decision


def _lookup_key(destination: str) -> str:
    return destination.strip().casefold()
