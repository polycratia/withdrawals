import logging
from decimal import Decimal

import pytest

from withdrawals import (
    Custody,
    InMemoryCustody,
    Rail,
    Route,
    WithdrawalRequest,
    route,
)

OURS = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"
ALSO_OURS = "0x742d35cc6634c0532925a3b844bc454e4438f44e"
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
    return InMemoryCustody({OURS: "acct-77", ALSO_OURS: "acct-88"})


DESTINATIONS = [
    pytest.param(THEIRS, Rail.EXTERNAL, None, id="an-address-we-do-not-know"),
    pytest.param(THEIRS.upper(), Rail.EXTERNAL, None, id="case-does-not-make-it-ours"),
    pytest.param(OURS, Rail.INTERNAL, "acct-77", id="an-address-we-custody"),
    pytest.param(ALSO_OURS, Rail.INTERNAL, "acct-88", id="another-account-of-ours"),
    pytest.param(OURS.upper(), Rail.INTERNAL, "acct-77", id="upper-case"),
    pytest.param(f"  {OURS}  ", Rail.INTERNAL, "acct-77", id="surrounding-space"),
    pytest.param(f" {OURS.title()}", Rail.INTERNAL, "acct-77", id="mixed-case-and-space"),
]


@pytest.mark.parametrize(("destination", "rail", "account"), DESTINATIONS)
def test_each_destination_goes_where_it_belongs(custody, destination, rail, account):
    decision = route(make(destination=destination), custody)

    assert decision.rail is rail
    assert decision.account == account
    assert decision.destination == destination
    assert decision.reason.strip()


@pytest.mark.parametrize(("destination", "rail", "account"), DESTINATIONS)
def test_the_rail_decides_how_the_transfer_settles(
    custody, destination, rail, account
):
    decision = route(make(destination=destination), custody)

    assert decision.is_internal is (rail is Rail.INTERNAL)
    assert decision.needs_confirmations is (rail is Rail.EXTERNAL)


@pytest.mark.parametrize("destination", [OURS, ALSO_OURS, THEIRS])
def test_without_custody_everything_goes_on_chain(destination):
    decision = route(make(destination=destination), InMemoryCustody({}))

    assert decision.rail is Rail.EXTERNAL
    assert decision.account is None


def test_an_internal_decision_names_the_account_it_credits(custody):
    decision = route(make(destination=ALSO_OURS), custody)

    assert "acct-88" in decision.reason


class SingleAccount:
    """Custody that knows exactly one deposit address."""

    def __init__(self, destination: str, account: str) -> None:
        self._destination = destination
        self._account = account

    def account_for(self, destination: str) -> str | None:
        return self._account if destination == self._destination else None


def test_any_custody_lookup_will_do():
    custody = SingleAccount(THEIRS, "acct-99")

    decision = route(make(destination=THEIRS), custody)

    assert isinstance(custody, Custody)
    assert decision.rail is Rail.INTERNAL
    assert decision.account == "acct-99"


@pytest.mark.parametrize(
    ("destination", "rail"),
    [
        pytest.param(OURS, "internal", id="internal"),
        pytest.param(THEIRS, "external", id="external"),
    ],
)
def test_every_decision_is_logged(custody, caplog, destination, rail):
    with caplog.at_level(logging.INFO, logger="withdrawals.routing"):
        route(make(destination=destination), custody)

    assert "w-1041" in caplog.text
    assert rail in caplog.text


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(
            {"rail": Rail.INTERNAL, "destination": OURS, "reason": "ours"},
            id="internal-without-an-account",
        ),
        pytest.param(
            {
                "rail": Rail.INTERNAL,
                "destination": OURS,
                "reason": "ours",
                "account": "   ",
            },
            id="internal-with-a-blank-account",
        ),
        pytest.param(
            {
                "rail": Rail.EXTERNAL,
                "destination": THEIRS,
                "reason": "theirs",
                "account": "acct-77",
            },
            id="external-with-an-account",
        ),
        pytest.param(
            {"rail": Rail.EXTERNAL, "destination": "   ", "reason": "theirs"},
            id="without-a-destination",
        ),
        pytest.param(
            {"rail": Rail.EXTERNAL, "destination": THEIRS, "reason": "   "},
            id="without-a-reason",
        ),
    ],
)
def test_an_impossible_route_is_refused(fields):
    with pytest.raises(ValueError):
        Route(**fields)


@pytest.mark.parametrize(
    "accounts",
    [
        pytest.param({OURS: "   "}, id="a-blank-account"),
        pytest.param({OURS: ""}, id="an-empty-account"),
        pytest.param({"   ": "acct-77"}, id="a-blank-address"),
    ],
)
def test_custody_entries_need_an_address_and_an_account(accounts):
    with pytest.raises(ValueError):
        InMemoryCustody(accounts)
