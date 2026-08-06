"""The gate-pulse CLI must build the exact outputCtrl the controller expects."""

import argparse

import pytest

from cli import trafix
from trafix.config import load_config
from trafix.protocol import METHOD_OUTPUT_CTRL

SERIAL_IN = "441D6491AF17"  # the real board at 192.168.1.204


class RecordingBus:
    def __init__(self):
        self.published = []

    def publish(self, topic, envelope):
        self.published.append((topic, envelope))

    def disconnect(self):
        pass


@pytest.fixture
def bus(monkeypatch):
    bus = RecordingBus()
    monkeypatch.setattr(trafix, "_bus", lambda config, client_id: bus)
    monkeypatch.setattr(trafix.time, "sleep", lambda seconds: None)
    return bus


@pytest.fixture
def config():
    return load_config("sim")


def _args(**overrides):
    base = dict(gate="1", relay="relay2", ms=500, beep_ms=0, exit_lane=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_gate_pulses_the_chosen_relay_on_the_entry_topic(bus, config):
    trafix.cmd_gate(_args(), config)

    (topic, envelope), = bus.published
    assert topic == "/GATE/IN/1"
    assert envelope.method == METHOD_OUTPUT_CTRL
    assert envelope.serial_no == SERIAL_IN
    assert envelope.data == {"relay2Out": [1, 500]}


def test_gate_accepts_the_wire_key_and_an_optional_beep(bus, config):
    trafix.cmd_gate(_args(relay="relay1Out", ms=1000, beep_ms=100), config)

    (_, envelope), = bus.published
    assert envelope.data == {"relay1Out": [1, 1000], "beepOut": [1, 100]}


def test_gate_exit_lane_targets_the_out_topic(bus, config):
    trafix.cmd_gate(_args(gate="2", relay="relay1", exit_lane=True), config)

    (topic, envelope), = bus.published
    assert topic == "/GATE/OUT/2"
    assert envelope.serial_no == "441D6491AF18"


def test_gate_without_a_configured_controller_warns_and_uses_empty_serial(
    bus, capsys
):
    config = load_config("site")  # site has no exit controller (§7.6)
    trafix.cmd_gate(_args(gate="2", relay="relay1", exit_lane=True), config)

    (topic, envelope), = bus.published
    assert topic == "/GATE/OUT/2"
    assert envelope.serial_no == ""
    assert "no controller configured for gate 2" in capsys.readouterr().err


def test_gate_rejects_unknown_relays(bus, config):
    with pytest.raises(SystemExit):
        trafix.cmd_gate(_args(relay="relay9"), config)


def test_out_gate_opens_the_exit_barrier(bus, config):
    trafix.cmd_out_gate(argparse.Namespace(gate="2"), config)

    (topic, envelope), = bus.published
    assert topic == "/GATE/OUT/2"
    assert envelope.method == METHOD_OUTPUT_CTRL
    assert envelope.serial_no == "441D6491AF18"
    assert envelope.data == {"relay1Out": [1, 1000], "beepOut": [1, 100]}


def test_card_sends_the_sim_command_to_the_controller(bus, config):
    args = argparse.Namespace(gate="1", card="006343040")
    trafix.cmd_card(args, config)

    (topic, envelope), = bus.published
    assert topic == "trafix/sim/controller/1"
    assert envelope.method == "card"
    assert envelope.get("card_no") == "006343040"
