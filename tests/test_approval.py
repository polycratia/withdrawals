import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from withdrawals import (
    Approval,
    ApprovalPolicy,
    ApprovalRule,
    InvalidTransition,
    NotApproved,
    SelfApproval,
    WithdrawalRequest,
    WithdrawalState,
    approve,
    ensure_approved,
)


def make(**changes) -> WithdrawalRequest:
    fields = {
        "id": "w-1041",
        "amount": Decimal("25.00"),
        "currency": "usdc",
        "destination": "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f",
        "requested_by": "alice",
    }
    fields.update(changes)
    return WithdrawalRequest(**fields)


@pytest.fixture
def policy() -> ApprovalPolicy:
    return ApprovalPolicy.four_eyes(above=Decimal("1000"))


def test_a_small_amount_clears_on_one_approval(policy):
    approved = approve(make(), "bob", policy)

    assert approved.state is WithdrawalState.APPROVED
    assert approved.approvers == ("bob",)
    assert approved.approvals[0].policy == "single-approver"


def test_past_the_threshold_it_takes_a_second_pair_of_eyes(policy):
    large = make(amount=Decimal("5000.00"))

    waiting = approve(large, "bob", policy)
    approved = approve(waiting, "carol", policy)

    assert waiting.state is WithdrawalState.REQUESTED
    assert approved.state is WithdrawalState.APPROVED
    assert approved.approvers == ("bob", "carol")
    assert {a.policy for a in approved.approvals} == {"four-eyes"}


def test_the_threshold_is_inclusive(policy):
    assert policy.match(Decimal("999.99")).approvals == 1
    assert policy.match(Decimal("1000")).approvals == 2


def test_an_approval_carries_who_and_when(policy):
    before = datetime.now(timezone.utc)

    approval = approve(make(), "bob", policy).approvals[0]

    assert approval.approver == "bob"
    assert before <= approval.at <= datetime.now(timezone.utc)


def test_a_given_moment_is_kept(policy):
    moment = datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc)

    approved = approve(make(), "bob", policy, at=moment)

    assert approved.approvals[0].at == moment


def test_a_naive_moment_is_refused():
    with pytest.raises(ValueError):
        Approval(approver="bob", at=datetime(2026, 9, 14), policy="four-eyes")


def test_an_approval_needs_an_approver():
    with pytest.raises(ValueError):
        Approval(approver="  ", at=datetime.now(timezone.utc), policy="rule")


def test_the_same_approver_twice_counts_once(policy):
    large = make(amount=Decimal("5000.00"))
    waiting = approve(large, "bob", policy)

    again = approve(waiting, "bob", policy)

    assert again is waiting
    assert again.state is WithdrawalState.REQUESTED
    assert len(again.approvals) == 1


def test_the_person_who_asked_cannot_approve(policy):
    with pytest.raises(SelfApproval):
        approve(make(), "alice", policy)


def test_an_approved_request_takes_no_further_approvals(policy):
    approved = approve(make(), "bob", policy)

    with pytest.raises(InvalidTransition):
        approve(approved, "carol", policy)


def test_the_same_approver_on_an_approved_request_changes_nothing(policy):
    approved = approve(make(), "bob", policy)

    assert approve(approved, "bob", policy) is approved


def test_approvals_travel_with_the_request(policy):
    approved = approve(make(), "bob", policy)

    confirmed = approved.start_sending().mark_sent("0xdeadbeef").confirm()

    assert confirmed.approvals == approved.approvals


def test_nothing_leaves_without_the_approvals_its_policy_asks_for(policy):
    with pytest.raises(NotApproved):
        ensure_approved(make(amount=Decimal("5000.00")), policy)


def test_a_tightened_policy_catches_an_old_approval(policy):
    approved = approve(make(), "bob", policy)
    stricter = ApprovalPolicy([ApprovalRule(name="everything", approvals=2)])

    assert ensure_approved(approved, policy) is approved
    with pytest.raises(NotApproved):
        ensure_approved(approved, stricter)


def test_a_policy_needs_a_rule_that_starts_at_zero():
    with pytest.raises(ValueError):
        ApprovalPolicy([ApprovalRule(name="big", approvals=2, at_least=Decimal("10"))])


def test_two_rules_cannot_start_at_the_same_amount():
    with pytest.raises(ValueError):
        ApprovalPolicy(
            [
                ApprovalRule(name="one", approvals=1),
                ApprovalRule(name="two", approvals=2),
            ]
        )


def test_an_empty_policy_is_refused():
    with pytest.raises(ValueError):
        ApprovalPolicy([])


def test_a_rule_must_ask_for_at_least_one_approval():
    with pytest.raises(ValueError):
        ApprovalRule(name="nobody", approvals=0)


def test_a_float_threshold_is_refused():
    with pytest.raises(TypeError):
        ApprovalRule(name="big", approvals=2, at_least=1000.0)


def test_a_request_cannot_carry_the_same_approver_twice():
    moment = datetime.now(timezone.utc)
    twice = (
        Approval(approver="bob", at=moment, policy="four-eyes"),
        Approval(approver="bob", at=moment, policy="four-eyes"),
    )

    with pytest.raises(ValueError):
        make(approvals=twice)


def test_the_approval_is_logged(policy, caplog):
    with caplog.at_level(logging.INFO, logger="withdrawals.approval"):
        approve(make(), "bob", policy)

    assert "w-1041" in caplog.text
    assert "single-approver" in caplog.text
