# withdrawals

Withdrawal pipeline: route internal transfers off-chain, send external ones
on-chain, stay idempotent when the callback arrives twice.

## Status

Pre-alpha. The withdrawal request, its state machine, the approval gate, the
routing decision, idempotent submission and the failure paths that return funds
are in place; the senders are not implemented yet.

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
    requested_by="alice",
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

## Approval

The amount decides how many people it takes. A policy is a set of rules, each
with the amount it starts at and the number of distinct approvers it asks for;
the rule with the highest threshold the amount clears is the one that applies.

```python
from withdrawals import ApprovalPolicy, approve, ensure_approved

policy = ApprovalPolicy.four_eyes(above=Decimal("1000"))

large = WithdrawalRequest(
    id="w-1042",
    amount=Decimal("5000.00"),
    currency="USDC",
    destination="0xab5801a7d398351b8be11c439e05c5b3259aec9b",
    requested_by="alice",
)

waiting = approve(large, "bob", policy)
assert waiting.state is WithdrawalState.REQUESTED

approved = approve(waiting, "carol", policy)
assert approved.state is WithdrawalState.APPROVED
assert approved.approvers == ("bob", "carol")
assert approved.approvals[0].policy == "four-eyes"
```

Every approval names who gave it, when they gave it (timezone-aware) and the
rule that was in force at the time, and it travels with the request all the way
to `confirmed`. The person who asked for the money cannot be one of the
approvers (`SelfApproval`), and the same approver recorded twice counts once,
so a click that arrives twice never becomes a second pair of eyes.

`request.approve()` is the bare state move; `approve(request, who, policy)` is
the one that asks the policy first. Build your own rules with `ApprovalRule`
when two tiers are not enough — a policy needs one rule starting at zero, so
that every amount matches exactly one.

Before handing a request to a rail, `ensure_approved(request, policy)` measures
it against the policy again and raises `NotApproved` when the signatures behind
it no longer suffice: thresholds change, and a request approved yesterday under
a looser rule should not slip out today.

## Routing

A withdrawal to an address we custody never reaches a chain: crediting it is a
ledger move — no fee, instant, nothing to confirm. `route` takes that decision,
returns it as a value with the reason behind it, and logs it, so the two paths
are never mistaken for one another after the fact.

```python
from withdrawals import InMemoryCustody, Rail, route

custody = InMemoryCustody({"0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f": "acct-77"})

decision = route(request, custody)

assert decision.rail is Rail.INTERNAL
assert decision.account == "acct-77"
assert not decision.needs_confirmations
```

Any destination custody does not recognise routes `EXTERNAL` and settles only
once the chain confirms it. Supply your own custody lookup by implementing
`account_for(destination) -> str | None`.

## Idempotent submission

A client that does not hear back retries. Every submission carries a request
key the client chooses: the first call under a key runs the send, every later
call returns what that first one produced, and the rail is touched once.

```python
from withdrawals import Submissions

submissions = Submissions()

def send(pending):
    return pending.approve().start_sending().mark_sent("0xdeadbeef")

first = submissions.submit("client-req-9f21", request, send)
again = submissions.submit("client-req-9f21", request, send)

assert again is first
```

The same key carrying a different withdrawal raises `IdempotencyConflict`
instead of paying twice. When the send itself raises, the key stays claimed and
a retry raises `SubmissionInFlight`, because nobody knows yet whether the rail
saw it: close it with `resolve(key, outcome)` once it has been reconciled, or
`release(key)` when it is certain nothing moved. The ledger lives in the
process that owns it.

## Failure paths

Money that does not leave has to come back. A rail that refuses a transaction
says so by raising `BroadcastRejected`; `broadcast` fails the request and
releases its hold in the same call.

```python
from withdrawals import BroadcastRejected, Hold, InMemoryHolds, broadcast

holds = InMemoryHolds()
holds.place(Hold.on(request))

def refused(pending):
    raise BroadcastRejected("insufficient gas")

failed = broadcast(request.approve(), refused, holds)

assert failed.state is WithdrawalState.FAILED
assert failed.reason == "insufficient gas"
assert holds.held(request.id) is None
```

Any other exception means nobody knows whether the transaction made it, so the
hold stays and the error travels on: releasing funds that may still be moving
is worse than holding them one reconciliation longer. Bring your own vault by
implementing `release(withdrawal_id) -> bool`.

A transaction that does not confirm is not forgotten either. `review` measures
how long it has been waiting against a policy and hands back the decision, with
its reason, as a value:

```python
from datetime import timedelta

from withdrawals import Action, StuckPolicy, bump, cancel, review

policy = StuckPolicy(bump_after=timedelta(minutes=10), cancel_after=timedelta(hours=2))

decision = review(sent, sent_at=broadcast_at, policy=policy)

if decision.action is Action.BUMP:
    sent = bump(sent, "0xfeedface")
elif decision.action is Action.CANCEL:
    sent = cancel(sent, "0xcafebabe", holds, reason=decision.reason)
```

A bump keeps the transaction it replaced in `replaced`, so the attempts behind
a send stay readable; a cancel records the replacement, fails the request and
returns the hold. Both are free to repeat: bumping to the reference a request
already carries returns the same request, and cancelling a cancelled one
releases nothing a second time. A transaction that has been bumped as often as
the policy allows is cancelled instead of bumped again, and an old reference
that comes back after being replaced raises `InvalidTransition`.

## Tests

```bash
python -m pytest
```

## License

MIT, see [LICENSE](LICENSE).

---

Maintained by [polycratia](https://polycratia.com).
