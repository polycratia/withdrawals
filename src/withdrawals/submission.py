"""Submitting a withdrawal once, however often the client asks for it.

A client that does not hear back retries. Every submission carries a request
key the client chooses: the first call under a key runs the send, and every
later call under that key returns the outcome the first one produced, so the
rail is never touched twice. A key that comes back with a different withdrawal
behind it is a conflict, not a second payment.

A key protects a retry, not against a second client sending the same
withdrawal. The ledger indexes the withdrawal id to the key that claimed it:
submitting or resolving an id under a second key is a conflict naming the first
one, and releasing a key frees its id again.

The ledger lives in the process that owns it. A send that raises leaves its key
claimed rather than free, because nobody knows yet whether the rail saw it:
`resolve` closes such a key once it has been reconciled, and `release` gives it
back, together with the withdrawal it held, when it is certain nothing moved.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from withdrawals.models import WithdrawalError, WithdrawalRequest

__all__ = [
    "IdempotencyConflict",
    "SubmissionInFlight",
    "Submissions",
]

logger = logging.getLogger(__name__)

_Fingerprint = tuple[str, Decimal, str, str]


class IdempotencyConflict(WithdrawalError):
    """A request key and the withdrawal behind it contradict the ledger."""


class SubmissionInFlight(WithdrawalError):
    """A request key is claimed and what it produced is not known yet."""


@dataclass(slots=True)
class _Record:
    fingerprint: _Fingerprint
    outcome: WithdrawalRequest | None = None


class Submissions:
    """A ledger of request keys, the withdrawal each claimed and its outcome."""

    __slots__ = ("_claims", "_lock", "_records")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, _Record] = {}
        self._claims: dict[str, str] = {}

    def submit(
        self,
        key: str,
        request: WithdrawalRequest,
        send: Callable[[WithdrawalRequest], WithdrawalRequest] | None = None,
    ) -> WithdrawalRequest:
        """Send `request` under `key`, or replay what that key already sent.

        `send` is whatever moves the money and returns the resulting request;
        without one the request is recorded as it stands.
        """
        key = _require_key(key)
        fingerprint = _fingerprint(request)
        replay = self._claim(key, fingerprint, request.id)
        if replay is not None:
            return replay
        try:
            outcome = request if send is None else send(request)
        except BaseException:
            logger.warning(
                "submission %s left withdrawal %s with an unknown outcome; "
                "the key stays claimed until it is resolved",
                key,
                request.id,
            )
            raise
        return self.resolve(key, outcome)

    def outcome(self, key: str) -> WithdrawalRequest | None:
        """What `key` produced, or None while it produced nothing yet."""
        key = _require_key(key)
        with self._lock:
            record = self._records.get(key)
            return None if record is None else record.outcome

    def claimed_by(self, withdrawal_id: str) -> str | None:
        """The request key that claimed `withdrawal_id`, or None."""
        if not isinstance(withdrawal_id, str) or not withdrawal_id.strip():
            raise ValueError("withdrawal id must be a non-empty string")
        with self._lock:
            return self._claims.get(withdrawal_id)

    def resolve(self, key: str, outcome: WithdrawalRequest) -> WithdrawalRequest:
        """Record what `key` produced, so later retries replay it."""
        key = _require_key(key)
        if not isinstance(outcome, WithdrawalRequest):
            raise TypeError("an outcome must be a WithdrawalRequest")
        fingerprint = _fingerprint(outcome)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                self._claim_id(key, outcome.id)
                self._records[key] = _Record(fingerprint, outcome)
            elif record.fingerprint != fingerprint:
                raise IdempotencyConflict(
                    f"request key {key} belongs to another withdrawal"
                )
            elif record.outcome is not None and record.outcome != outcome:
                raise IdempotencyConflict(
                    f"request key {key} already produced a different outcome"
                )
            else:
                record.outcome = outcome
        logger.info(
            "submission %s recorded withdrawal %s as %s",
            key,
            outcome.id,
            outcome.state,
        )
        return outcome

    def release(self, key: str) -> None:
        """Give a claimed key back, once it is certain nothing was sent."""
        key = _require_key(key)
        with self._lock:
            record = self._records.get(key)
            if record is not None:
                if record.outcome is not None:
                    raise IdempotencyConflict(
                        f"request key {key} already produced an outcome"
                    )
                withdrawal_id = record.fingerprint[0]
                if self._claims.get(withdrawal_id) == key:
                    del self._claims[withdrawal_id]
            self._records.pop(key, None)
        logger.info("submission %s released", key)

    def _claim(
        self, key: str, fingerprint: _Fingerprint, request_id: str
    ) -> WithdrawalRequest | None:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                self._claim_id(key, request_id)
                self._records[key] = _Record(fingerprint)
                return None
            if record.fingerprint != fingerprint:
                raise IdempotencyConflict(
                    f"request key {key} belongs to another withdrawal"
                )
            if record.outcome is None:
                raise SubmissionInFlight(
                    f"request key {key} is claimed and its outcome is not known yet"
                )
            outcome = record.outcome
        logger.info("submission %s replayed for withdrawal %s", key, request_id)
        return outcome

    def _claim_id(self, key: str, withdrawal_id: str) -> None:
        holder = self._claims.get(withdrawal_id)
        if holder is not None and holder != key:
            raise IdempotencyConflict(
                f"withdrawal {withdrawal_id} is claimed by request key {holder}"
            )
        self._claims[withdrawal_id] = key


def _fingerprint(request: WithdrawalRequest) -> _Fingerprint:
    return (request.id, request.amount, request.currency, request.destination)


def _require_key(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("request key must be a non-empty string")
    return key.strip()
