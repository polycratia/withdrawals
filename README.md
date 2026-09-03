# withdrawals

Withdrawal pipeline: route internal transfers off-chain, send external ones
on-chain, stay idempotent when the callback arrives twice.

## Status

Pre-alpha. The withdrawal request and its state machine are in place; routing
and the senders are not implemented yet.

## Installation

```bash
pip install withdrawals
```

From a checkout:

```bash
pip install -e .
```

## Usage

A request travels `requested -> approved -> sending -> sent -> confirmed`, with
`rejected` (only while the money has not moved) and `failed` as the two ways
out. A request is an immutable value: transitions return a new request and the
caller decides where to store it.

```python
from decimal import Decimal

from withdrawals import WithdrawalRequest, WithdrawalState

request = WithdrawalRequest(
    id="w-1041",
    amount=Decimal("25.00"),
    currency="USDC",
    destination="0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f",
)

sent = request.approve().start_sending().mark_sent("0xdeadbeef")
confirmed = sent.confirm()

assert confirmed.state is WithdrawalState.CONFIRMED
assert confirmed.is_terminal
```

Replaying a transition that already happened returns the same value, so a
callback delivered twice costs nothing:

```python
assert sent.mark_sent("0xdeadbeef") is sent
```

A replay carrying different data is a conflict, not a silent overwrite, and
raises `InvalidTransition` — as does any step the state machine does not allow.

## Tests

```bash
python -m pytest
```

## License

MIT, see [LICENSE](LICENSE).

---

Maintained by [polycratia](https://polycratia.com).
