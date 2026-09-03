from decimal import Decimal

import pytest

from withdrawals import InvalidTransition, WithdrawalRequest, WithdrawalState


def make(**changes) -> WithdrawalRequest:
    fields = {
        "id": "w-1041",
        "amount": Decimal("25.00"),
        "currency": "usdc",
        "destination": "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f",
    }
    fields.update(changes)
    return WithdrawalRequest(**fields)


def test_new_request_starts_as_requested():
    request = make()

    assert request.state is WithdrawalState.REQUESTED
    assert not request.is_terminal


def test_currency_is_normalised_to_upper_case():
    assert make().currency == "USDC"


def test_float_amounts_are_refused():
    with pytest.raises(TypeError):
        make(amount=25.0)


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": Decimal("0")},
        {"amount": Decimal("-1")},
        {"currency": "$"},
        {"destination": "  "},
        {"id": ""},
    ],
)
def test_invalid_fields_are_refused(changes):
    with pytest.raises(ValueError):
        make(**changes)


def test_happy_path_reaches_confirmed():
    request = make()

    sent = request.approve().start_sending().mark_sent("0xdeadbeef")
    confirmed = sent.confirm()

    assert confirmed.state is WithdrawalState.CONFIRMED
    assert confirmed.reference == "0xdeadbeef"
    assert confirmed.is_terminal


def test_transitions_leave_the_original_untouched():
    request = make()

    request.approve()

    assert request.state is WithdrawalState.REQUESTED


def test_repeated_callbacks_are_free():
    sent = make().approve().start_sending().mark_sent("0xdeadbeef")

    assert sent.mark_sent("0xdeadbeef") is sent
    assert sent.confirm().confirm() is not None
    assert sent.confirm().confirm().state is WithdrawalState.CONFIRMED


def test_a_second_reference_for_the_same_send_is_a_conflict():
    sent = make().approve().start_sending().mark_sent("0xdeadbeef")

    with pytest.raises(InvalidTransition):
        sent.mark_sent("0xfeedface")


def test_rejection_is_possible_until_sending_starts():
    approved = make().approve()

    rejected = approved.reject("limit exceeded")

    assert rejected.state is WithdrawalState.REJECTED
    assert rejected.reason == "limit exceeded"
    assert rejected.reject("limit exceeded") is rejected


def test_rejection_after_sending_started_is_refused():
    sending = make().approve().start_sending()

    with pytest.raises(InvalidTransition):
        sending.reject("too late")


def test_a_send_can_fail():
    sending = make().approve().start_sending()

    failed = sending.fail("rpc timeout")

    assert failed.state is WithdrawalState.FAILED
    assert failed.reason == "rpc timeout"
    assert failed.is_terminal


def test_terminal_states_do_not_move():
    confirmed = make().approve().start_sending().mark_sent("0xdeadbeef").confirm()

    with pytest.raises(InvalidTransition):
        confirmed.fail("nope")


def test_skipping_a_step_is_refused():
    with pytest.raises(InvalidTransition):
        make().mark_sent("0xdeadbeef")
