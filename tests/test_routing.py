import logging
from decimal import Decimal

import pytest

from withdrawals import InMemoryCustody, Rail, Route, WithdrawalRequest, route

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


@pytest.fixture
def custody() -> InMemoryCustody:
    return InMemoryCustody({OURS: "acct-77"})


def test_an_address_we_custody_goes_off_chain(custody):
    decision = route(make(destination=OURS), custody)

    assert decision.rail is Rail.INTERNAL
    assert decision.account == "acct-77"
    assert decision.is_internal
    assert not decision.needs_confirmations


def test_anything_else_goes_on_chain(custody):
    decision = route(make(), custody)

    assert decision.rail is Rail.EXTERNAL
    assert decision.account is None
    assert decision.needs_confirmations


def test_custody_lookup_ignores_case_and_padding(custody):
    decision = route(make(destination=f"  {OURS.upper()}  "), custody)

    assert decision.rail is Rail.INTERNAL


def test_every_decision_carries_a_reason(custody):
    internal = route(make(destination=OURS), custody)
    external = route(make(), custody)

    assert "acct-77" in internal.reason
    assert external.reason.strip()


def test_the_decision_is_logged(custody, caplog):
    with caplog.at_level(logging.INFO, logger="withdrawals.routing"):
        route(make(destination=OURS), custody)

    assert "w-1041" in caplog.text
    assert "internal" in caplog.text


def test_an_internal_route_without_an_account_is_refused():
    with pytest.raises(ValueError):
        Route(rail=Rail.INTERNAL, destination=OURS, reason="ours")


def test_an_external_route_with_an_account_is_refused():
    with pytest.raises(ValueError):
        Route(
            rail=Rail.EXTERNAL,
            destination=THEIRS,
            reason="theirs",
            account="acct-77",
        )


def test_custody_entries_need_an_address_and_an_account():
    with pytest.raises(ValueError):
        InMemoryCustody({OURS: "  "})
