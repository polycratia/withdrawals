import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from withdrawals import (
    Action,
    BroadcastRejected,
    Hold,
    HoldMismatch,
    InMemoryHolds,
    InvalidTransition,
    StuckPolicy,
    WithdrawalRequest,
    WithdrawalState,
    broadcast,
    bump,
    cancel,
    return_funds,
    review,
)

SENT_AT = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def make(**changes) -> WithdrawalRequest:
    fields = {
        "id": "w-1041",
        "amount": Decimal("25.00"),
        "currency": "usdc",
        "destination": "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f",
    }
    fields.update(changes)
    return WithdrawalRequest(**fields)


def sent(**changes) -> WithdrawalRequest:
    return make(**changes).approve().start_sending().mark_sent("0xdeadbeef")


def refuse(pending: WithdrawalRequest) -> WithdrawalRequest:
    raise BroadcastRejected("insufficient gas")


def hang(pending: WithdrawalRequest) -> WithdrawalRequest:
    raise RuntimeError("rpc timeout")


def succeed(pending: WithdrawalRequest) -> WithdrawalRequest:
    return pending.mark_sent("0xdeadbeef")


@pytest.fixture
def holds() -> InMemoryHolds:
    return InMemoryHolds([Hold.on(make())])


@pytest.fixture
def policy() -> StuckPolicy:
    return StuckPolicy(bump_after=timedelta(minutes=10), cancel_after=timedelta(hours=2))


def test_a_refused_broadcast_releases_the_hold(holds):
    failed = broadcast(make().approve(), refuse, holds)

    assert failed.state is WithdrawalState.FAILED
    assert failed.reason == "insufficient gas"
    assert holds.held("w-1041") is None


def test_an_unknown_outcome_keeps_the_hold(holds):
    with pytest.raises(RuntimeError):
        broadcast(make().approve(), hang, holds)

    assert holds.held("w-1041").amount == Decimal("25.00")


def test_a_broadcast_that_goes_through_leaves_the_hold_alone(holds):
    outcome = broadcast(make().approve(), succeed, holds)

    assert outcome.state is WithdrawalState.SENT
    assert outcome.reference == "0xdeadbeef"
    assert holds.held("w-1041") is not None


def test_the_send_sees_a_request_that_is_on_its_way(holds):
    seen = []

    def watch(pending: WithdrawalRequest) -> WithdrawalRequest:
        seen.append(pending.state)
        return pending.mark_sent("0xdeadbeef")

    broadcast(make().approve(), watch, holds)

    assert seen == [WithdrawalState.SENDING]


def test_a_send_must_return_a_request(holds):
    with pytest.raises(TypeError):
        broadcast(make().approve(), lambda pending: "0xdeadbeef", holds)


def test_an_unapproved_request_never_reaches_the_rail(holds):
    with pytest.raises(InvalidTransition):
        broadcast(make(), succeed, holds)


def test_funds_are_returned_once_however_often_it_is_asked(holds):
    failed = return_funds(make().approve(), "the rail refused it", holds)
    again = return_funds(failed, "the rail refused it", holds)

    assert again is failed
    assert holds.held("w-1041") is None


def test_a_hold_released_twice_reports_the_second_time_as_nothing(holds):
    assert holds.release("w-1041") is True
    assert holds.release("w-1041") is False


def test_the_same_hold_placed_twice_is_free(holds):
    assert holds.place(Hold.on(make())) == Hold.on(make())


def test_a_hold_with_other_numbers_is_refused(holds):
    with pytest.raises(HoldMismatch):
        holds.place(Hold.on(make(amount=Decimal("26.00"))))


def test_a_fresh_transaction_is_left_alone(policy):
    decision = review(
        sent(), sent_at=SENT_AT, policy=policy, now=SENT_AT + timedelta(minutes=5)
    )

    assert decision.action is Action.WAIT
    assert decision.is_wait
    assert decision.waited == timedelta(minutes=5)


def test_past_the_deadline_it_is_bumped(policy):
    decision = review(
        sent(), sent_at=SENT_AT, policy=policy, now=SENT_AT + timedelta(minutes=30)
    )

    assert decision.action is Action.BUMP
    assert decision.reason.strip()


def test_long_past_the_deadline_it_is_cancelled(policy):
    decision = review(
        sent(), sent_at=SENT_AT, policy=policy, now=SENT_AT + timedelta(hours=3)
    )

    assert decision.action is Action.CANCEL


def test_a_transaction_bumped_as_often_as_allowed_is_cancelled():
    policy = StuckPolicy(
        bump_after=timedelta(minutes=10), cancel_after=timedelta(hours=2), bumps=1
    )
    bumped = bump(sent(), "0xfeedface")

    decision = review(
        bumped, sent_at=SENT_AT, policy=policy, now=SENT_AT + timedelta(minutes=30)
    )

    assert decision.action is Action.CANCEL


def test_only_a_transaction_on_the_chain_can_be_stuck(policy):
    with pytest.raises(InvalidTransition):
        review(make().approve(), sent_at=SENT_AT, policy=policy)


def test_a_naive_moment_is_refused(policy):
    with pytest.raises(ValueError):
        review(sent(), sent_at=datetime(2026, 9, 14, 12, 0), policy=policy)


def test_a_bump_records_the_transaction_it_replaced():
    bumped = bump(sent(), "0xfeedface")

    assert bumped.state is WithdrawalState.SENT
    assert bumped.reference == "0xfeedface"
    assert bumped.replaced == ("0xdeadbeef",)


def test_bumping_to_the_reference_it_already_carries_changes_nothing():
    already = sent()

    assert bump(already, "0xdeadbeef") is already


def test_a_replaced_transaction_cannot_come_back():
    bumped = bump(sent(), "0xfeedface")

    with pytest.raises(InvalidTransition):
        bump(bumped, "0xdeadbeef")


def test_nothing_but_a_sent_transaction_can_be_bumped():
    with pytest.raises(InvalidTransition):
        bump(make().approve(), "0xfeedface")


def test_a_cancel_replaces_the_transaction_and_returns_the_hold(holds):
    cancelled = cancel(sent(), "0xcafebabe", holds, reason="stuck for two hours")

    assert cancelled.state is WithdrawalState.FAILED
    assert cancelled.reference == "0xcafebabe"
    assert cancelled.replaced == ("0xdeadbeef",)
    assert cancelled.reason == "stuck for two hours"
    assert holds.held("w-1041") is None


def test_cancelling_twice_is_free(holds):
    cancelled = cancel(sent(), "0xcafebabe", holds, reason="stuck for two hours")

    assert cancel(cancelled, "0xcafebabe", holds, reason="stuck for two hours") is cancelled


@pytest.mark.parametrize(
    "changes",
    [
        {"bump_after": timedelta(0)},
        {"cancel_after": timedelta(minutes=1)},
        {"bumps": 0},
    ],
)
def test_an_impossible_policy_is_refused(changes):
    fields = {"bump_after": timedelta(minutes=10), "cancel_after": timedelta(hours=2)}
    fields.update(changes)

    with pytest.raises(ValueError):
        StuckPolicy(**fields)


def test_a_policy_is_made_of_timedeltas():
    with pytest.raises(TypeError):
        StuckPolicy(bump_after=600, cancel_after=timedelta(hours=2))


def test_the_decision_is_logged(policy, caplog):
    with caplog.at_level(logging.WARNING, logger="withdrawals.recovery"):
        review(
            sent(), sent_at=SENT_AT, policy=policy, now=SENT_AT + timedelta(minutes=30)
        )

    assert "w-1041" in caplog.text
    assert "bump" in caplog.text
