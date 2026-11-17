"""Edge cases and failure modes of the approval, routing, submission and
recovery steps, kept apart from the tests that read as documentation."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from withdrawals import (
    Action,
    Approval,
    BroadcastRejected,
    Hold,
    HoldMismatch,
    Holds,
    IdempotencyConflict,
    InMemoryHolds,
    InvalidTransition,
    Rail,
    Recovery,
    StuckPolicy,
    SubmissionInFlight,
    Submissions,
    WithdrawalRequest,
    WithdrawalState,
    broadcast,
    bump,
    cancel,
    return_funds,
    review,
)

SENT_AT = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
THEIRS = "0xab5801a7d398351b8be11c439e05c5b3259aec9b"


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


def approval(approver: str = "bob") -> Approval:
    return Approval(
        approver=approver, at=datetime.now(timezone.utc), policy="single-approver"
    )


@pytest.fixture
def holds() -> InMemoryHolds:
    return InMemoryHolds([Hold.on(make())])


@pytest.fixture
def policy() -> StuckPolicy:
    return StuckPolicy(bump_after=timedelta(minutes=10), cancel_after=timedelta(hours=2))


@pytest.mark.parametrize("transition", ["reject", "fail"])
def test_a_replay_carrying_another_reason_is_a_conflict(transition):
    left = getattr(make().approve(), transition)("limit exceeded")

    assert getattr(left, transition)("limit exceeded") is left
    with pytest.raises(InvalidTransition):
        getattr(left, transition)("something else entirely")


@pytest.mark.parametrize(
    ("transition", "argument"),
    [
        pytest.param("reject", "   ", id="a-blank-rejection"),
        pytest.param("fail", "", id="an-empty-failure"),
        pytest.param("mark_sent", "  ", id="a-blank-reference"),
    ],
)
def test_a_transition_that_explains_itself_needs_the_words(transition, argument):
    with pytest.raises(ValueError):
        getattr(make().approve(), transition)(argument)


def test_nothing_fails_or_confirms_before_its_turn():
    with pytest.raises(InvalidTransition):
        make().fail("rpc timeout")
    with pytest.raises(InvalidTransition):
        make().approve().confirm()


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"replaced": ("",)}, id="an-empty-reference"),
        pytest.param({"replaced": ("   ",)}, id="a-blank-reference"),
        pytest.param({"replaced": ("tx-1", "tx-1")}, id="the-same-reference-twice"),
        pytest.param(
            {"reference": "tx-1", "replaced": ("tx-1",), "state": "sent"},
            id="current-and-replaced-at-once",
        ),
    ],
)
def test_an_impossible_history_of_attempts_is_refused(changes):
    with pytest.raises(ValueError):
        make(**changes)


def test_the_attempts_behind_a_send_stay_in_order():
    twice = sent().bump("tx-2").bump("tx-3")

    assert twice.reference == "tx-3"
    assert twice.replaced == ("0xdeadbeef", "tx-2")


def test_a_state_or_rail_written_out_is_the_value_itself():
    assert make(state="sent", reference="0xdeadbeef").state is WithdrawalState.SENT
    assert make(rail="internal").rail is Rail.INTERNAL
    with pytest.raises(ValueError):
        make(state="posted")
    with pytest.raises(ValueError):
        make(rail="ledger")


def test_the_rail_travels_all_the_way_into_a_failure():
    failed = make().routed(Rail.EXTERNAL).approve().start_sending().fail("rpc timeout")

    assert failed.rail is Rail.EXTERNAL


def test_approvals_must_be_approvals():
    with pytest.raises(TypeError):
        make(approvals=("bob",))
    with pytest.raises(TypeError):
        make().record_approval("bob")


def test_an_approval_already_kept_is_free_whatever_the_state():
    given = approval()
    approved = make().record_approval(given).approve()

    assert approved.record_approval(given) is approved
    with pytest.raises(InvalidTransition):
        approved.record_approval(approval("carol"))


def test_the_person_who_asked_is_kept_without_surrounding_space():
    assert make(requested_by="  alice  ").requested_by == "alice"
    with pytest.raises(ValueError):
        make(requested_by="   ")


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"amount": Decimal("NaN")}, id="an-amount-that-is-not-one"),
        pytest.param({"currency": "x"}, id="a-ticker-of-one-letter"),
        pytest.param({"currency": "toolongaticker"}, id="a-ticker-too-long"),
        pytest.param({"currency": 7}, id="a-currency-that-is-a-number"),
        pytest.param({"id": "   "}, id="a-blank-id"),
        pytest.param({"destination": None}, id="no-destination"),
    ],
)
def test_more_invalid_fields_are_refused(changes):
    with pytest.raises(ValueError):
        make(**changes)


@pytest.mark.parametrize("amount", ["25.00", None, 25])
def test_an_amount_is_a_decimal_or_nothing(amount):
    with pytest.raises(TypeError):
        make(amount=amount)


def test_the_bump_deadline_is_inclusive(policy):
    decision = review(
        sent(), sent_at=SENT_AT, policy=policy, now=SENT_AT + timedelta(minutes=10)
    )

    assert decision.action is Action.BUMP
    assert decision.waited == timedelta(minutes=10)


def test_a_moment_before_the_bump_deadline_still_waits(policy):
    decision = review(
        sent(),
        sent_at=SENT_AT,
        policy=policy,
        now=SENT_AT + timedelta(minutes=10) - timedelta(microseconds=1),
    )

    assert decision.action is Action.WAIT


def test_the_cancel_deadline_is_inclusive_too(policy):
    decision = review(
        sent(), sent_at=SENT_AT, policy=policy, now=SENT_AT + timedelta(hours=2)
    )

    assert decision.action is Action.CANCEL
    assert not decision.is_wait


def test_a_policy_that_cancels_as_soon_as_it_bumps_never_bumps():
    tight = StuckPolicy(
        bump_after=timedelta(minutes=10), cancel_after=timedelta(minutes=10)
    )

    at_ten = review(
        sent(), sent_at=SENT_AT, policy=tight, now=SENT_AT + timedelta(minutes=10)
    )
    at_nine = review(
        sent(), sent_at=SENT_AT, policy=tight, now=SENT_AT + timedelta(minutes=9)
    )

    assert at_ten.action is Action.CANCEL
    assert at_nine.action is Action.WAIT


def test_a_clock_that_runs_backwards_waits(policy):
    decision = review(
        sent(), sent_at=SENT_AT, policy=policy, now=SENT_AT - timedelta(minutes=1)
    )

    assert decision.action is Action.WAIT
    assert decision.waited == -timedelta(minutes=1)


def test_a_moment_in_another_zone_is_measured_all_the_same(policy):
    elsewhere = datetime(2026, 9, 14, 14, 0, tzinfo=timezone(timedelta(hours=2)))

    decision = review(
        sent(), sent_at=elsewhere, policy=policy, now=SENT_AT + timedelta(minutes=30)
    )

    assert decision.action is Action.BUMP
    assert decision.waited == timedelta(minutes=30)


def test_a_bumped_transaction_with_attempts_left_is_bumped_again(policy):
    decision = review(
        bump(sent(), "0xfeedface"),
        sent_at=SENT_AT,
        policy=policy,
        now=SENT_AT + timedelta(minutes=30),
    )

    assert decision.action is Action.BUMP


@pytest.mark.parametrize(
    "state",
    [
        pytest.param("confirmed", id="already-confirmed"),
        pytest.param("failed", id="already-failed"),
        pytest.param("sending", id="still-on-its-way"),
    ],
)
def test_nothing_but_a_waiting_transaction_is_reviewed(policy, state):
    moved = {
        "confirmed": lambda: sent().confirm(),
        "failed": lambda: sent().fail("rpc timeout"),
        "sending": lambda: make().approve().start_sending(),
    }[state]()

    with pytest.raises(InvalidTransition):
        review(moved, sent_at=SENT_AT, policy=policy)


def test_a_naive_now_is_refused_as_well(policy):
    with pytest.raises(ValueError):
        review(
            sent(), sent_at=SENT_AT, policy=policy, now=datetime(2026, 9, 14, 13, 0)
        )


@pytest.mark.parametrize("moment", ["2026-09-14T12:00:00Z", 0, timedelta(hours=1)])
def test_a_moment_has_to_be_a_datetime(policy, moment):
    with pytest.raises(TypeError):
        review(sent(), sent_at=moment, policy=policy, now=SENT_AT)


def test_a_bump_does_not_count_the_attempts_itself():
    ridden = sent()
    for reference in ("tx-2", "tx-3", "tx-4", "tx-5"):
        ridden = bump(ridden, reference)

    assert ridden.reference == "tx-5"
    assert ridden.replaced == ("0xdeadbeef", "tx-2", "tx-3", "tx-4")


def test_bumping_needs_a_reference():
    with pytest.raises(ValueError):
        bump(sent(), "   ")


def test_a_cancel_brings_a_reason_of_its_own(holds):
    cancelled = cancel(sent(), "0xcafebabe", holds)

    assert cancelled.reason == "the transaction was cancelled before it confirmed"


def test_cancelling_the_transaction_it_already_carries_replaces_nothing(holds):
    cancelled = cancel(sent(), "0xdeadbeef", holds, reason="stuck for two hours")

    assert cancelled.state is WithdrawalState.FAILED
    assert cancelled.reference == "0xdeadbeef"
    assert cancelled.replaced == ()
    assert holds.held("w-1041") is None


def test_a_cancelled_request_takes_no_second_replacement(holds):
    cancelled = cancel(sent(), "0xcafebabe", holds, reason="stuck for two hours")

    with pytest.raises(InvalidTransition):
        cancel(cancelled, "0xbadc0ffee", holds, reason="stuck again")

    assert cancelled.reference == "0xcafebabe"


def test_cancelling_a_request_with_no_transaction_leaves_its_hold(holds):
    with pytest.raises(InvalidTransition):
        cancel(make().approve(), "0xcafebabe", holds, reason="stuck for two hours")

    assert holds.held("w-1041") is not None


def test_a_refusal_without_a_word_still_names_a_reason(holds):
    def refuse_without_a_word(pending: WithdrawalRequest) -> WithdrawalRequest:
        raise BroadcastRejected()

    failed = broadcast(make().approve(), refuse_without_a_word, holds)

    assert failed.reason == "the rail refused the broadcast"
    assert holds.held("w-1041") is None


def test_an_interrupted_broadcast_keeps_the_hold(holds):
    def interrupt(pending: WithdrawalRequest) -> WithdrawalRequest:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        broadcast(make().approve(), interrupt, holds)

    assert holds.held("w-1041") is not None


def test_a_failure_with_nothing_held_behind_it_is_still_a_failure(holds):
    failed = return_funds(make(id="w-1042").approve(), "the rail refused it", holds)

    assert failed.state is WithdrawalState.FAILED
    assert holds.held("w-1041") is not None


def test_a_vault_of_your_own_is_enough():
    class Vault:
        def __init__(self) -> None:
            self.released: list[str] = []

        def release(self, withdrawal_id: str) -> bool:
            self.released.append(withdrawal_id)
            return self.released.count(withdrawal_id) == 1

    vault = Vault()

    def refuse(pending: WithdrawalRequest) -> WithdrawalRequest:
        raise BroadcastRejected("insufficient gas")

    failed = broadcast(make().approve(), refuse, vault)

    assert isinstance(vault, Holds)
    assert failed.state is WithdrawalState.FAILED
    assert vault.released == ["w-1041"]


def test_an_empty_vault_holds_nothing():
    assert InMemoryHolds().held("w-1041") is None
    assert InMemoryHolds().release("w-1041") is False


def test_a_hold_in_another_currency_is_refused(holds):
    with pytest.raises(HoldMismatch):
        holds.place(
            Hold(withdrawal_id="w-1041", amount=Decimal("25.00"), currency="EURC")
        )

    assert holds.held("w-1041").currency == "USDC"


def test_placing_a_hold_takes_a_hold(holds):
    with pytest.raises(TypeError):
        holds.place("w-1041")


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"withdrawal_id": "   "}, id="without-an-id"),
        pytest.param({"amount": Decimal("0")}, id="nothing-to-hold"),
        pytest.param({"amount": Decimal("-1")}, id="a-negative-amount"),
        pytest.param({"amount": Decimal("NaN")}, id="an-amount-that-is-not-one"),
        pytest.param({"currency": "$"}, id="a-currency-that-is-not-a-ticker"),
        pytest.param({"currency": ""}, id="without-a-currency"),
    ],
)
def test_an_impossible_hold_is_refused(changes):
    fields = {
        "withdrawal_id": "w-1041",
        "amount": Decimal("25.00"),
        "currency": "USDC",
    }
    fields.update(changes)

    with pytest.raises(ValueError):
        Hold(**fields)


@pytest.mark.parametrize("amount", [25.0, "25.00", None, 25])
def test_a_hold_is_measured_in_decimals(amount):
    with pytest.raises(TypeError):
        Hold(withdrawal_id="w-1041", amount=amount, currency="USDC")


def test_a_hold_normalises_the_currency_it_names():
    kept = Hold(withdrawal_id="w-1041", amount=Decimal("25.00"), currency="usdc")

    assert kept == Hold.on(make())


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"bump_after": timedelta(minutes=-1)}, id="a-bump-in-the-past"),
        pytest.param({"cancel_after": timedelta(0)}, id="a-cancel-with-no-patience"),
        pytest.param({"bumps": -1}, id="fewer-bumps-than-none"),
        pytest.param({"bumps": True}, id="a-count-that-is-a-flag"),
        pytest.param({"bumps": 1.5}, id="half-a-bump"),
    ],
)
def test_more_impossible_policies_are_refused(changes):
    fields = {"bump_after": timedelta(minutes=10), "cancel_after": timedelta(hours=2)}
    fields.update(changes)

    with pytest.raises(ValueError):
        StuckPolicy(**fields)


@pytest.mark.parametrize("cancel_after", [7200, "2h"])
def test_a_cancel_deadline_is_a_timedelta(cancel_after):
    with pytest.raises(TypeError):
        StuckPolicy(bump_after=timedelta(minutes=10), cancel_after=cancel_after)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        pytest.param({"action": "bump"}, TypeError, id="an-action-in-words"),
        pytest.param({"reason": "   "}, ValueError, id="without-a-reason"),
        pytest.param({"waited": 1800}, TypeError, id="a-wait-in-seconds"),
    ],
)
def test_an_impossible_recovery_is_refused(changes, error):
    fields = {
        "action": Action.BUMP,
        "reason": "unconfirmed for half an hour",
        "waited": timedelta(minutes=30),
    }
    fields.update(changes)

    with pytest.raises(error):
        Recovery(**fields)


def send_once(request: WithdrawalRequest) -> WithdrawalRequest:
    return request.approve().start_sending().mark_sent("0xdeadbeef")


def boom(request: WithdrawalRequest) -> WithdrawalRequest:
    raise RuntimeError("rpc timeout")


def test_a_retry_is_recognised_whatever_state_the_request_is_read_in():
    ledger = Submissions()
    first = ledger.submit("req-1", make(), send_once)

    assert ledger.submit("req-1", make().approve(), send_once) is first


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"id": "w-1042"}, id="another-withdrawal"),
        pytest.param({"amount": Decimal("26.00")}, id="another-amount"),
        pytest.param({"currency": "eurc"}, id="another-currency"),
        pytest.param({"destination": THEIRS}, id="another-destination"),
    ],
)
def test_a_key_is_bound_to_the_money_behind_it(changes):
    ledger = Submissions()
    first = ledger.submit("req-1", make(), send_once)

    with pytest.raises(IdempotencyConflict):
        ledger.submit("req-1", make(**changes), send_once)

    assert ledger.outcome("req-1") is first


def test_a_send_that_comes_back_with_another_withdrawal_is_a_conflict():
    ledger = Submissions()

    def swap(request: WithdrawalRequest) -> WithdrawalRequest:
        return send_once(make(id="w-9999"))

    with pytest.raises(IdempotencyConflict):
        ledger.submit("req-1", make(), swap)

    assert ledger.outcome("req-1") is None
    assert ledger.claimed_by("w-9999") is None
    with pytest.raises(SubmissionInFlight):
        ledger.submit("req-1", make(), send_once)


def test_a_send_that_returns_nothing_leaves_the_key_claimed():
    ledger = Submissions()

    with pytest.raises(TypeError):
        ledger.submit("req-1", make(), lambda request: None)

    assert ledger.outcome("req-1") is None
    assert ledger.claimed_by("w-1041") == "req-1"


def test_an_interrupted_send_keeps_the_key_claimed():
    ledger = Submissions()

    def interrupt(request: WithdrawalRequest) -> WithdrawalRequest:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ledger.submit("req-1", make(), interrupt)

    with pytest.raises(SubmissionInFlight):
        ledger.submit("req-1", make(), send_once)
    assert ledger.claimed_by("w-1041") == "req-1"


def test_resolving_the_same_outcome_again_is_free():
    ledger = Submissions()
    first = ledger.submit("req-1", make(), send_once)

    assert ledger.resolve("req-1", first) is first
    assert ledger.outcome("req-1") is first


def test_an_outcome_read_back_from_storage_resolves_too():
    ledger = Submissions()
    first = ledger.submit("req-1", make(), send_once)
    twin = send_once(make())

    assert twin == first
    assert twin is not first
    assert ledger.resolve("req-1", twin) is twin
    assert ledger.outcome("req-1") == first


@pytest.mark.parametrize("outcome", ["0xdeadbeef", None, 7])
def test_an_outcome_must_be_a_request(outcome):
    with pytest.raises(TypeError):
        Submissions().resolve("req-1", outcome)


def test_a_key_claimed_with_space_around_it_is_the_same_key():
    ledger = Submissions()
    with pytest.raises(RuntimeError):
        ledger.submit(" req-1 ", make(), boom)

    assert ledger.claimed_by("w-1041") == "req-1"

    ledger.release("req-1  ")

    assert ledger.claimed_by("w-1041") is None
    assert ledger.outcome("req-1") is None


def test_a_released_withdrawal_can_be_resolved_under_a_second_key():
    ledger = Submissions()
    with pytest.raises(RuntimeError):
        ledger.submit("req-1", make(), boom)
    ledger.release("req-1")

    found = send_once(make())
    ledger.resolve("req-2", found)

    assert ledger.claimed_by("w-1041") == "req-2"
    assert ledger.outcome("req-2") is found
    assert ledger.outcome("req-1") is None


def test_releasing_a_key_nobody_claimed_is_free():
    ledger = Submissions()

    ledger.release("req-1")

    assert ledger.outcome("req-1") is None
    assert ledger.claimed_by("w-1041") is None
