import logging
import threading
import time
from decimal import Decimal

import pytest

from withdrawals import (
    IdempotencyConflict,
    SubmissionInFlight,
    Submissions,
    WithdrawalRequest,
    WithdrawalState,
)


def make(**changes) -> WithdrawalRequest:
    fields = {
        "id": "w-1041",
        "amount": Decimal("25.00"),
        "currency": "usdc",
        "destination": "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f",
    }
    fields.update(changes)
    return WithdrawalRequest(**fields)


class Sender:
    """A send that counts how often the rail was actually touched."""

    def __init__(self, reference: str = "0xdeadbeef", delay: float = 0.0) -> None:
        self.calls = 0
        self._reference = reference
        self._delay = delay
        self._lock = threading.Lock()

    def __call__(self, request: WithdrawalRequest) -> WithdrawalRequest:
        with self._lock:
            self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        return request.approve().start_sending().mark_sent(self._reference)


class Failing:
    """A send that counts its attempts and never comes back with an outcome."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self._delay = delay
        self._lock = threading.Lock()

    def __call__(self, request: WithdrawalRequest) -> WithdrawalRequest:
        with self._lock:
            self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        raise RuntimeError("rpc timeout")


def boom(request: WithdrawalRequest) -> WithdrawalRequest:
    raise RuntimeError("rpc timeout")


def race(count, task):
    """Run `task(index)` in `count` threads released at the same moment."""
    start = threading.Barrier(count)
    lock = threading.Lock()
    outcomes: list = []
    errors: list[Exception] = []

    def worker(index: int) -> None:
        start.wait()
        try:
            outcome = task(index)
        except Exception as error:
            with lock:
                errors.append(error)
        else:
            with lock:
                outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes, errors


def test_the_first_submission_sends():
    sender = Sender()

    outcome = Submissions().submit("req-1", make(), sender)

    assert sender.calls == 1
    assert outcome.state is WithdrawalState.SENT


def test_a_retry_replays_the_first_outcome():
    ledger = Submissions()
    sender = Sender()

    first = ledger.submit("req-1", make(), sender)
    again = ledger.submit("req-1", make(), sender)

    assert again is first
    assert sender.calls == 1


def test_another_withdrawal_under_another_key_sends_again():
    ledger = Submissions()
    sender = Sender()

    ledger.submit("req-1", make(), sender)
    ledger.submit("req-2", make(id="w-1042"), sender)

    assert sender.calls == 2


def test_a_key_is_recognised_through_surrounding_space():
    ledger = Submissions()
    sender = Sender()

    first = ledger.submit("req-1", make(), sender)

    assert ledger.submit("  req-1  ", make(), sender) is first


def test_reusing_a_key_for_another_withdrawal_is_a_conflict():
    ledger = Submissions()
    sender = Sender()
    ledger.submit("req-1", make(), sender)

    with pytest.raises(IdempotencyConflict):
        ledger.submit("req-1", make(amount=Decimal("26.00")), sender)

    assert sender.calls == 1


def test_a_second_key_for_a_claimed_withdrawal_is_a_conflict():
    ledger = Submissions()
    sender = Sender()
    ledger.submit("req-1", make(), sender)

    with pytest.raises(IdempotencyConflict) as conflict:
        ledger.submit("req-2", make(), sender)

    assert "req-1" in str(conflict.value)
    assert sender.calls == 1
    assert ledger.outcome("req-2") is None


def test_a_second_key_cannot_resolve_a_claimed_withdrawal():
    ledger = Submissions()
    ledger.submit("req-1", make(), Sender())
    found = make().approve().start_sending().mark_sent("0xdeadbeef")

    with pytest.raises(IdempotencyConflict) as conflict:
        ledger.resolve("req-2", found)

    assert "req-1" in str(conflict.value)
    assert ledger.outcome("req-2") is None


def test_a_claimed_withdrawal_names_the_key_holding_it():
    ledger = Submissions()
    ledger.submit("req-1", make(), Sender())

    assert ledger.claimed_by("w-1041") == "req-1"
    assert ledger.claimed_by("w-1042") is None


def test_a_withdrawal_in_flight_is_claimed_as_firmly_as_a_sent_one():
    ledger = Submissions()
    sender = Sender()
    with pytest.raises(RuntimeError):
        ledger.submit("req-1", make(), boom)

    with pytest.raises(IdempotencyConflict):
        ledger.submit("req-2", make(), sender)

    assert sender.calls == 0


def test_without_a_send_the_request_is_recorded_as_it_stands():
    ledger = Submissions()
    request = make()

    assert ledger.submit("req-1", request) is request
    assert ledger.outcome("req-1") is request


def test_a_send_that_raises_keeps_the_key_claimed():
    ledger = Submissions()
    sender = Sender()

    with pytest.raises(RuntimeError):
        ledger.submit("req-1", make(), boom)

    with pytest.raises(SubmissionInFlight):
        ledger.submit("req-1", make(), sender)

    assert sender.calls == 0
    assert ledger.outcome("req-1") is None


def test_a_claimed_key_closes_once_the_truth_is_known():
    ledger = Submissions()
    sender = Sender()
    with pytest.raises(RuntimeError):
        ledger.submit("req-1", make(), boom)

    found = make().approve().start_sending().mark_sent("0xdeadbeef")
    ledger.resolve("req-1", found)

    assert ledger.submit("req-1", make(), sender) is found
    assert sender.calls == 0


def test_resolving_with_another_outcome_is_a_conflict():
    ledger = Submissions()
    ledger.submit("req-1", make(), Sender())

    with pytest.raises(IdempotencyConflict):
        ledger.resolve("req-1", make().approve())


def test_releasing_a_claimed_key_allows_a_retry():
    ledger = Submissions()
    sender = Sender()
    with pytest.raises(RuntimeError):
        ledger.submit("req-1", make(), boom)

    ledger.release("req-1")
    outcome = ledger.submit("req-1", make(), sender)

    assert sender.calls == 1
    assert outcome.state is WithdrawalState.SENT


def test_releasing_a_key_frees_the_withdrawal_for_another_key():
    ledger = Submissions()
    sender = Sender()
    with pytest.raises(RuntimeError):
        ledger.submit("req-1", make(), boom)

    ledger.release("req-1")

    assert ledger.claimed_by("w-1041") is None
    assert ledger.submit("req-2", make(), sender).state is WithdrawalState.SENT
    assert ledger.claimed_by("w-1041") == "req-2"
    assert sender.calls == 1


def test_releasing_a_key_that_produced_an_outcome_is_refused():
    ledger = Submissions()
    ledger.submit("req-1", make(), Sender())

    with pytest.raises(IdempotencyConflict):
        ledger.release("req-1")

    assert ledger.claimed_by("w-1041") == "req-1"


def test_an_unknown_key_has_no_outcome():
    assert Submissions().outcome("req-1") is None


@pytest.mark.parametrize("key", ["", "   ", None, 7])
def test_every_entry_point_needs_a_real_key(key):
    ledger = Submissions()

    with pytest.raises(ValueError):
        ledger.submit(key, make())
    with pytest.raises(ValueError):
        ledger.outcome(key)
    with pytest.raises(ValueError):
        ledger.resolve(key, make())
    with pytest.raises(ValueError):
        ledger.release(key)


@pytest.mark.parametrize("withdrawal_id", ["", "   ", None, 7])
def test_a_claim_lookup_needs_a_real_withdrawal_id(withdrawal_id):
    with pytest.raises(ValueError):
        Submissions().claimed_by(withdrawal_id)


def test_a_send_must_return_a_request():
    with pytest.raises(TypeError):
        Submissions().submit("req-1", make(), lambda request: "0xdeadbeef")


def test_a_replay_is_logged(caplog):
    ledger = Submissions()
    ledger.submit("req-1", make(), Sender())

    with caplog.at_level(logging.INFO, logger="withdrawals.submission"):
        ledger.submit("req-1", make(), Sender())

    assert "req-1" in caplog.text


def test_a_double_submit_under_one_key_touches_the_rail_once():
    ledger = Submissions()
    request = make()
    sender = Sender(delay=0.05)

    outcomes, errors = race(8, lambda _: ledger.submit("req-1", request, sender))

    assert sender.calls == 1
    assert len(outcomes) + len(errors) == 8
    assert outcomes
    assert all(outcome is outcomes[0] for outcome in outcomes)
    assert all(isinstance(error, SubmissionInFlight) for error in errors)
    assert ledger.outcome("req-1") is outcomes[0]


def test_two_withdrawals_racing_for_one_key_leave_one_conflict():
    ledger = Submissions()
    sender = Sender(delay=0.05)
    requests = [make(), make(id="w-1042")]

    outcomes, errors = race(2, lambda i: ledger.submit("req-1", requests[i], sender))

    assert sender.calls == 1
    assert len(outcomes) == 1
    assert [type(error) for error in errors] == [IdempotencyConflict]


def test_two_keys_racing_for_one_withdrawal_leave_one_send():
    ledger = Submissions()
    sender = Sender(delay=0.05)

    outcomes, errors = race(2, lambda i: ledger.submit(f"req-{i}", make(), sender))

    assert sender.calls == 1
    assert len(outcomes) == 1
    assert [type(error) for error in errors] == [IdempotencyConflict]
    assert ledger.claimed_by("w-1041") in {"req-0", "req-1"}


def test_a_send_that_raises_under_load_keeps_the_key_from_everyone():
    ledger = Submissions()
    failing = Failing(delay=0.05)

    outcomes, errors = race(6, lambda _: ledger.submit("req-1", make(), failing))

    raised = [type(error) for error in errors]
    assert not outcomes
    assert failing.calls == 1
    assert raised.count(RuntimeError) == 1
    assert raised.count(SubmissionInFlight) == 5
    assert ledger.outcome("req-1") is None


def test_keys_racing_against_each_other_each_send_once():
    ledger = Submissions()
    sender = Sender(delay=0.01)

    outcomes, errors = race(
        6, lambda i: ledger.submit(f"req-{i}", make(id=f"w-{i}"), sender)
    )

    assert not errors
    assert sender.calls == 6
    assert len({outcome.id for outcome in outcomes}) == 6
