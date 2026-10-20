"""Who may let a withdrawal leave, and how many of them it takes.

The amount decides. A policy is a set of rules, each with the amount it starts
at and the number of distinct people it asks for: small withdrawals clear on a
single signature, anything past the threshold needs a second pair of eyes, and
the person who asked for the money is never one of them.

Thresholds are amounts of a currency, not bare numbers. A policy names the
currency it is written in and refuses a withdrawal in another one, so a rule
set at a thousand of a stablecoin never clears a thousand of a dearer coin.

Every approval is kept with the request — who gave it, when, and which rule was
in force at the time. The same approver recorded twice counts once, so a click
that arrives twice never becomes a second pair of eyes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from withdrawals.models import Approval, WithdrawalError, WithdrawalRequest

__all__ = [
    "ApprovalPolicy",
    "ApprovalRule",
    "NotApproved",
    "PolicyMismatch",
    "SelfApproval",
    "approve",
    "ensure_approved",
]

logger = logging.getLogger(__name__)


class NotApproved(WithdrawalError):
    """A withdrawal was about to leave without the approvals it needs."""


class PolicyMismatch(WithdrawalError):
    """A policy was measured against a withdrawal in another currency."""


class SelfApproval(WithdrawalError):
    """The person who asked for a withdrawal tried to approve it."""


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    """How many people it takes, from the amount this rule starts at."""

    name: str
    approvals: int
    at_least: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("a rule must be named")
        if (
            not isinstance(self.approvals, int)
            or isinstance(self.approvals, bool)
            or self.approvals < 1
        ):
            raise ValueError("a rule must ask for at least one approval")
        if isinstance(self.at_least, float):
            raise TypeError("at_least must be a Decimal, not a float")
        if not isinstance(self.at_least, Decimal):
            raise TypeError("at_least must be a Decimal")
        if not self.at_least.is_finite() or self.at_least < 0:
            raise ValueError("at_least must be a non-negative finite Decimal")
        object.__setattr__(self, "name", self.name.strip())


class ApprovalPolicy:
    """The rules in force, read from the highest threshold down.

    The thresholds are amounts of `currency`; a withdrawal in another one is
    not something this policy can measure.
    """

    __slots__ = ("_currency", "_rules")

    def __init__(self, rules: Iterable[ApprovalRule], currency: str) -> None:
        if (
            not isinstance(currency, str)
            or not currency.isalnum()
            or not 2 <= len(currency) <= 12
        ):
            raise ValueError("currency must be an alphanumeric ticker")
        given = tuple(rules)
        for rule in given:
            if not isinstance(rule, ApprovalRule):
                raise TypeError("a policy is made of ApprovalRule values")
        if not given:
            raise ValueError("a policy needs at least one rule")
        if len({rule.at_least for rule in given}) != len(given):
            raise ValueError("two rules cannot start at the same amount")
        ordered = sorted(given, key=lambda rule: rule.at_least, reverse=True)
        if ordered[-1].at_least != 0:
            raise ValueError(
                "a policy needs a rule starting at zero, so every amount matches"
            )
        self._currency: str = currency.upper()
        self._rules: tuple[ApprovalRule, ...] = tuple(ordered)

    @property
    def currency(self) -> str:
        """The currency the thresholds are amounts of."""
        return self._currency

    @property
    def rules(self) -> tuple[ApprovalRule, ...]:
        """The rules, highest threshold first."""
        return self._rules

    def match(self, amount: Decimal) -> ApprovalRule:
        """The rule that decides `amount`, read as an amount of `currency`."""
        for rule in self._rules[:-1]:
            if amount >= rule.at_least:
                return rule
        return self._rules[-1]

    def rule_for(self, request: WithdrawalRequest) -> ApprovalRule:
        """The rule that decides `request`, or refuse another currency."""
        if request.currency != self._currency:
            raise PolicyMismatch(
                f"withdrawal {request.id} is in {request.currency} and this "
                f"policy measures amounts of {self._currency}"
            )
        return self.match(request.amount)

    @classmethod
    def four_eyes(cls, above: Decimal, currency: str) -> ApprovalPolicy:
        """One signature up to `above` of `currency`, two distinct people beyond."""
        return cls(
            [
                ApprovalRule(name="single-approver", approvals=1),
                ApprovalRule(name="four-eyes", approvals=2, at_least=above),
            ],
            currency=currency,
        )


def approve(
    request: WithdrawalRequest,
    approver: str,
    policy: ApprovalPolicy,
    *,
    at: datetime | None = None,
) -> WithdrawalRequest:
    """Record one approval, and approve the request once the policy is met."""
    rule = policy.rule_for(request)
    approval = Approval(
        approver=approver,
        at=datetime.now(timezone.utc) if at is None else at,
        policy=rule.name,
    )
    if approval.approver in request.approvers:
        logger.info(
            "withdrawal %s already carries the approval of %s",
            request.id,
            approval.approver,
        )
        return request
    if approval.approver == request.requested_by:
        raise SelfApproval(
            f"withdrawal {request.id} cannot be approved by "
            f"{approval.approver}, who asked for it"
        )
    gathered = request.record_approval(approval)
    if len(gathered.approvals) < rule.approvals:
        logger.info(
            "withdrawal %s carries %d of the %d approvals policy %s asks for",
            gathered.id,
            len(gathered.approvals),
            rule.approvals,
            rule.name,
        )
        return gathered
    approved = gathered.approve()
    logger.info(
        "withdrawal %s approved under policy %s by %s",
        approved.id,
        rule.name,
        ", ".join(approved.approvers),
    )
    return approved


def ensure_approved(
    request: WithdrawalRequest, policy: ApprovalPolicy
) -> WithdrawalRequest:
    """Return `request` if `policy` is satisfied, or refuse to let it leave."""
    rule = policy.rule_for(request)
    if len(request.approvals) < rule.approvals:
        raise NotApproved(
            f"withdrawal {request.id} carries {len(request.approvals)} "
            f"approvals and policy {rule.name} asks for {rule.approvals}"
        )
    return request
