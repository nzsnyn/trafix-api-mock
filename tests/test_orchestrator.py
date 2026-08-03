"""Unit tests for the orchestrator's on-site testing modes.

These exercise the MQTT event dispatcher directly, so no broker or API is
needed: ``start()`` is never called and all outbound effects are stubbed.
"""

from __future__ import annotations

import threading

import pytest

from trafix.config import load_config
from trafix.orchestrator import LaneState, Orchestrator
from trafix.protocol import input_info, read_card


@pytest.fixture
def orche(monkeypatch):
    def build(*, rfid_only: bool):
        config = load_config("e2e")
        orchestrator = Orchestrator(config, vehicle_id=1, rfid_only=rfid_only)
        orchestrator.lanes["1"] = LaneState()
        orchestrator._locks["1"] = threading.Lock()
        published = []
        monkeypatch.setattr(
            orchestrator.bus, "publish_raw", lambda *a, **k: published.append(a)
        )
        handler = orchestrator._make_event_handler("1")
        return orchestrator, handler, published

    return build


def test_rfid_only_ignores_arrival(orche):
    _, handler, published = orche(rfid_only=True)
    handler("g/event/1", input_info("441D6491AF17", input3=1))
    assert published == []


def test_rfid_only_ignores_ticket_button(orche):
    _, handler, published = orche(rfid_only=True)
    handler("g/event/1", input_info("441D6491AF17", input2=1))
    assert published == []


def test_full_mode_still_reacts_to_arrival(orche):
    _, handler, published = orche(rfid_only=False)
    handler("g/event/1", input_info("441D6491AF17", input3=1))
    assert len(published) == 1


def test_rfid_only_still_delivers_read_card(orche, monkeypatch):
    orchestrator, handler, published = orche(rfid_only=True)
    seen = []
    monkeypatch.setattr(
        orchestrator,
        "_request_member_entry",
        lambda gate, card_no, serial_no: seen.append((gate, card_no)) or None,
    )
    handler("g/event/1", read_card("441D6491AF17", "006343040"))
    assert seen == [("1", "006343040")]
    assert published == []
