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

    def __init__(self, reference: str = "0xdeadbeef") -> None:
        self.calls = 0
        self._reference = reference

    def __call__(self, request: WithdrawalRequest) -> WithdrawalRequest:
        self.calls += 1
        return request.approve().start_sending().mark_sent(self._reference)


def boom(request: WithdrawalRequest) -> WithdrawalRequest:
    raise RuntimeError("rpc timeout")


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


def test_another_key_sends_again():
    ledger = Submissions()
    sender = Sender()

    ledger.submit("req-1", make(), sender)
    ledger.submit("req-2", make(), sender)

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


def test_releasing_a_key_that_produced_an_outcome_is_refused():
    ledger = Submissions()
    ledger.submit("req-1", make(), Sender())

    with pytest.raises(IdempotencyConflict):
        ledger.release("req-1")


def test_an_unknown_key_has_no_outcome():
    assert Submissions().outcome("req-1") is None


@pytest.mark.parametrize("key", ["", "   ", None, 7])
def test_a_key_must_be_a_non_empty_string(key):
    with pytest.raises(ValueError):
        Submissions().submit(key, make())


def test_a_send_must_return_a_request():
    with pytest.raises(TypeError):
        Submissions().submit("req-1", make(), lambda request: "0xdeadbeef")


def test_a_replay_is_logged(caplog):
    ledger = Submissions()
    ledger.submit("req-1", make(), Sender())

    with caplog.at_level(logging.INFO, logger="withdrawals.submission"):
        ledger.submit("req-1", make(), Sender())

    assert "req-1" in caplog.text


def test_parallel_retries_send_once():
    ledger = Submissions()
    request = make()
    sent = []
    outcomes = []
    in_flight = []
    start = threading.Barrier(8)

    def send(pending: WithdrawalRequest) -> WithdrawalRequest:
        time.sleep(0.05)
        sent.append(pending)
        return pending.approve().start_sending().mark_sent("0xdeadbeef")

    def worker() -> None:
        start.wait()
        try:
            outcomes.append(ledger.submit("req-1", request, send))
        except SubmissionInFlight:
            in_flight.append(True)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(sent) == 1
    assert len(outcomes) + len(in_flight) == 8
    assert all(outcome is outcomes[0] for outcome in outcomes)
