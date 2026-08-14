"""One-time short-lived WS tickets (CONTRACTS.md WS protocol; spec §8).

No tokens in query strings: POST /api/encounters issues a ticket, the client
sends it as the first WS text frame ({"type":"session.start", ...}). In-memory
store — single backend process demo; a restart invalidates outstanding tickets,
which is acceptable and honest for this demo.
"""

from __future__ import annotations

import secrets
import threading
import time

TICKET_TTL_SECONDS = 120.0

_lock = threading.Lock()
_tickets: dict[str, tuple[str, float]] = {}  # ticket -> (encounter_id, issued_at)


def issue_ticket(encounter_id: str) -> str:
    ticket = secrets.token_urlsafe(24)
    with _lock:
        _tickets[ticket] = (encounter_id, time.monotonic())
    return ticket


def redeem_ticket(ticket: str, encounter_id: str) -> bool:
    """Single use: valid tickets are consumed on first redemption."""
    with _lock:
        entry = _tickets.pop(ticket, None)
    if entry is None:
        return False
    ticket_encounter_id, issued_at = entry
    if time.monotonic() - issued_at > TICKET_TTL_SECONDS:
        return False
    return ticket_encounter_id == encounter_id


def _clear_all_for_tests() -> None:
    with _lock:
        _tickets.clear()
