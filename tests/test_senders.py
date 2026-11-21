import logging
from decimal import Decimal

import pytest

from withdrawals import (
    BroadcastRejected,
    Chain,
    Hold,
    IdempotencyConflict,
    InMemoryCustody,
    InMemoryHolds,
    InMemoryLedger,
    InvalidTransition,
    Ledger,
    LedgerEntry,
    Rail,
    Rails,
    Route,
    Submissions,
    WithdrawalRequest,
    WithdrawalState,
    broadcast,
    route,
    send_external,
    send_internal,
)

OURS = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"
THEIRS = "0xab5801a7d398351b8be11c439e05c5b3259aec9b"


def make(**changes) -> WithdrawalRequest:
    fields = {
        "id": "w-1041",
        "amount": Decimal("25.00"),
        "currency": "usdc",
        "destination": THEIRS,
    }
    fields.update(changes)
    return WithdrawalRequest(**fields)


class Wire:
    """A chain that records what it was asked to broadcast."""

    def __init__(self, reference: str = "0xdeadbeef") -> None:
        self.calls: list[tuple] = []
        self._reference = reference

    def send(
        self, destination: str, amount: Decimal, currency: str, *, withdrawal_id: str
    ) -> str:
        self.calls.append((destination, amount, currency, withdrawal_id))
        return self._reference


class Frozen:
    """A ledger that refuses every credit."""

    def credit(
        self, account: str, amount: Decimal, currency: str, *, withdrawal_id: str
    ) -> str:
        raise BroadcastRejected("the account is frozen")


class Silent:
    """A chain nobody hears back from."""

    def send(
        self, destination: str, amount: Decimal, currency: str, *, withdrawal_id: str
    ) -> str:
        raise RuntimeError("rpc timeout")


@pytest.fixture
def custody() -> InMemoryCustody:
    return InMemoryCustody({OURS: "acct-77"})


@pytest.fixture
def ledger() -> InMemoryLedger:
    return InMemoryLedger()


@pytest.fixture
def chain() -> Wire:
    return Wire()


@pytest.fixture
def holds() -> InMemoryHolds:
    return InMemoryHolds([Hold.on(make())])


def test_an_internal_send_credits_the_account_and_confirms(custody, ledger):
    request = make(destination=OURS)
    decision = route(request, custody)

    booked = send_internal(request.approve(), decision, ledger)

    assert booked.state is WithdrawalState.CONFIRMED
    assert booked.reference == "ledger-1"
    assert booked.rail is Rail.INTERNAL
    entry = ledger.entry_for("w-1041")
    assert entry.account == "acct-77"
    assert entry.amount == Decimal("25.00")
    assert entry.currency == "USDC"


def test_an_external_send_keeps_the_transaction_and_waits(chain):
    sent = send_external(make().approve(), chain)

    assert sent.state is WithdrawalState.SENT
    assert sent.reference == "0xdeadbeef"
    assert sent.rail is Rail.EXTERNAL
    assert chain.calls == [(THEIRS, Decimal("25.00"), "USDC", "w-1041")]


def test_the_decision_picks_the_send(custody, ledger, chain):
    rails = Rails(ledger=ledger, chain=chain)
    inside = make(destination=OURS)
    outside = make(id="w-1042")

    booked = rails.sender(route(inside, custody))(inside.approve())
    sent = rails.sender(route(outside, custody))(outside.approve())

    assert booked.state is WithdrawalState.CONFIRMED
    assert sent.state is WithdrawalState.SENT
    assert chain.calls == [(THEIRS, Decimal("25.00"), "USDC", "w-1042")]
    assert len(ledger.entries) == 1


def test_a_send_is_chosen_by_a_route(ledger, chain):
    with pytest.raises(TypeError):
        Rails(ledger, chain).sender(Rail.INTERNAL)


def test_a_send_does_not_cross_to_the_other_rail(custody, ledger, chain):
    inside = make(destination=OURS).routed(Rail.INTERNAL).approve()
    outside = make().routed(Rail.EXTERNAL).approve()
    decision = route(make(destination=OURS), custody)

    with pytest.raises(InvalidTransition):
        send_external(inside, chain)
    with pytest.raises(InvalidTransition):
        send_internal(outside, decision, ledger)

    assert chain.calls == []
    assert ledger.entries == ()


def test_an_internal_send_needs_the_account_its_route_names(ledger):
    external = Route(rail=Rail.EXTERNAL, destination=THEIRS, reason="theirs")

    with pytest.raises(ValueError):
        send_internal(make().approve(), external, ledger)


def test_an_unapproved_request_reaches_neither_rail(custody, ledger, chain):
    decision = route(make(destination=OURS), custody)

    with pytest.raises(InvalidTransition):
        send_external(make(), chain)
    with pytest.raises(InvalidTransition):
        send_internal(make(destination=OURS), decision, ledger)

    assert chain.calls == []
    assert ledger.entries == ()


def test_a_ledger_that_refuses_gives_the_hold_back(custody, chain, holds):
    request = make(destination=OURS)
    decision = route(request, custody)
    send = Rails(ledger=Frozen(), chain=chain).sender(decision)

    failed = broadcast(request.approve(), send, holds)

    assert failed.state is WithdrawalState.FAILED
    assert failed.reason == "the account is frozen"
    assert holds.held("w-1041") is None


def test_a_chain_nobody_hears_from_keeps_the_hold(holds):
    def send(pending: WithdrawalRequest) -> WithdrawalRequest:
        return send_external(pending, Silent())

    with pytest.raises(RuntimeError):
        broadcast(make().approve(), send, holds)

    assert holds.held("w-1041") is not None


def test_a_rail_that_hands_back_nothing_is_refused():
    class Mute:
        def send(self, destination, amount, currency, *, withdrawal_id) -> str:
            return "   "

    with pytest.raises(ValueError):
        send_external(make().approve(), Mute())


def test_a_retry_under_one_key_credits_once(custody, ledger, chain, holds):
    submissions = Submissions()
    request = make(destination=OURS)
    decision = route(request, custody)
    request = request.routed(decision).approve()
    send = Rails(ledger, chain).sender(decision)

    first = submissions.submit(
        "req-1", request, lambda pending: broadcast(pending, send, holds)
    )
    again = submissions.submit(
        "req-1", request, lambda pending: broadcast(pending, send, holds)
    )

    assert again is first
    assert first.state is WithdrawalState.CONFIRMED
    assert len(ledger.entries) == 1


def test_the_same_credit_asked_for_twice_is_booked_once(ledger):
    first = ledger.credit("acct-77", Decimal("25.00"), "usdc", withdrawal_id="w-1041")
    again = ledger.credit("acct-77", Decimal("25.00"), "USDC", withdrawal_id="w-1041")

    assert again == first
    assert len(ledger.entries) == 1


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"account": "acct-88"}, id="another-account"),
        pytest.param({"amount": Decimal("26.00")}, id="another-amount"),
        pytest.param({"currency": "eurc"}, id="another-currency"),
    ],
)
def test_a_credit_with_other_numbers_behind_it_is_a_conflict(ledger, changes):
    fields = {"account": "acct-77", "amount": Decimal("25.00"), "currency": "USDC"}
    ledger.credit(**fields, withdrawal_id="w-1041")
    fields.update(changes)

    with pytest.raises(IdempotencyConflict):
        ledger.credit(**fields, withdrawal_id="w-1041")

    assert len(ledger.entries) == 1


def test_entries_are_numbered_in_the_order_they_are_booked(ledger):
    ledger.credit("acct-77", Decimal("1"), "USDC", withdrawal_id="w-1")
    ledger.credit("acct-88", Decimal("2"), "USDC", withdrawal_id="w-2")

    assert [entry.reference for entry in ledger.entries] == ["ledger-1", "ledger-2"]
    assert ledger.entry_for("w-3") is None


def test_a_ledger_can_name_its_entries_itself():
    booked = InMemoryLedger("book").credit(
        "acct-77", Decimal("1"), "USDC", withdrawal_id="w-1"
    )

    assert booked == "book-1"


@pytest.mark.parametrize("prefix", ["", "   ", None, 7])
def test_a_ledger_prefix_has_to_be_words(prefix):
    with pytest.raises(ValueError):
        InMemoryLedger(prefix)


@pytest.mark.parametrize("withdrawal_id", ["", "   ", None, 7])
def test_an_entry_lookup_needs_a_real_withdrawal_id(withdrawal_id):
    with pytest.raises(ValueError):
        InMemoryLedger().entry_for(withdrawal_id)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"reference": "   "}, id="without-a-reference"),
        pytest.param({"withdrawal_id": ""}, id="without-a-withdrawal"),
        pytest.param({"account": "  "}, id="without-an-account"),
        pytest.param({"amount": Decimal("0")}, id="nothing-to-credit"),
        pytest.param({"amount": Decimal("NaN")}, id="an-amount-that-is-not-one"),
        pytest.param({"currency": "$"}, id="a-currency-that-is-not-a-ticker"),
    ],
)
def test_an_impossible_entry_is_refused(changes):
    fields = {
        "reference": "ledger-1",
        "withdrawal_id": "w-1041",
        "account": "acct-77",
        "amount": Decimal("25.00"),
        "currency": "USDC",
    }
    fields.update(changes)

    with pytest.raises(ValueError):
        LedgerEntry(**fields)


@pytest.mark.parametrize("amount", [25.0, "25.00", None])
def test_a_credit_is_measured_in_decimals(ledger, amount):
    with pytest.raises(TypeError):
        ledger.credit("acct-77", amount, "USDC", withdrawal_id="w-1041")


def test_a_ledger_or_a_chain_of_your_own_is_enough(ledger, chain):
    assert isinstance(ledger, Ledger)
    assert isinstance(chain, Chain)
    assert isinstance(Frozen(), Ledger)
    assert isinstance(Silent(), Chain)


def test_both_sends_are_logged(custody, ledger, chain, caplog):
    request = make(destination=OURS)
    decision = route(request, custody)

    with caplog.at_level(logging.INFO, logger="withdrawals.senders"):
        send_internal(request.approve(), decision, ledger)
        send_external(make(id="w-1042").approve(), chain)

    assert "acct-77" in caplog.text
    assert "ledger-1" in caplog.text
    assert "w-1042" in caplog.text
    assert "0xdeadbeef" in caplog.text
