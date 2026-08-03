"""Constants and message shapes here were observed on the wire (flow.md §8).
These tests exist to stop them drifting."""

import json

import pytest

from trafix import protocol
from trafix.protocol import (
    METHOD_INPUT_INFO,
    METHOD_OUTPUT_CTRL,
    METHOD_TX_UART_DATA,
    Envelope,
    ProtocolError,
    parse,
)

SERIAL = "441D6491AF17"  # the real gate controller at 192.168.1.204


# -- topics -----------------------------------------------------------------


def test_entry_topics_match_the_capture():
    assert protocol.gate_event_topic(1) == "/GATE/event/1"
    assert protocol.gate_in_topic(1) == "/GATE/IN/1"
    assert protocol.gate_status_topic(1) == "/GATE/IN/1/status"


def test_exit_lpr_topic_keeps_the_sites_lowercase_convention():
    """The exit LPR really does use a different convention. Do not 'fix' it."""
    assert protocol.gate_out_pos_topic(1) == "gate/out/1/pos"


# -- envelope ---------------------------------------------------------------


def test_envelope_serialises_the_documented_fields():
    envelope = Envelope(
        method=METHOD_TX_UART_DATA, serial_no=SERIAL, data=[{"uartNo": 2}], id="abc"
    )
    decoded = json.loads(envelope.to_json())
    assert set(decoded) == {"id", "serialNo", "version", "taskNo", "method", "data"}
    assert decoded["serialNo"] == SERIAL
    assert decoded["version"] == "1.0"
    assert decoded["taskNo"] == 2


def test_round_trip():
    original = Envelope(method=METHOD_INPUT_INFO, serial_no=SERIAL, data={"input3": 1})
    restored = parse(original.to_json())
    assert restored.method == original.method
    assert restored.serial_no == SERIAL
    assert restored.get("input3") == 1


def test_parses_a_real_barrier_command():
    raw = (
        '{"method":"outputCtrl","serialNo":"441D6491AF17","taskNo":1,'
        '"version":"1.0","data":{"beepOut":[1,100],"relay1Out":[1,1000]}}'
    )
    envelope = parse(raw)
    assert envelope.method == METHOD_OUTPUT_CTRL
    assert envelope.get("relay1Out") == [1, 1000]


def test_parses_an_ack_whose_code_is_a_string():
    envelope = parse('{"method":"txUartData","serialNo":"X","data":{"code":"0"}}')
    assert envelope.get("code") == "0"


@pytest.mark.parametrize(
    "raw", ["not json", "[]", '{"serialNo":"X"}', '{"method":""}']
)
def test_malformed_payloads_are_rejected(raw):
    with pytest.raises(ProtocolError):
        parse(raw)


def test_data_may_be_a_list_and_get_stays_safe():
    envelope = parse('{"method":"txUartData","serialNo":"X","data":[{"uartNo":2}]}')
    assert isinstance(envelope.data, list)
    assert envelope.get("anything") is None


# -- builders ---------------------------------------------------------------


def test_message_id_reproduces_the_php_md5_scheme():
    import hashlib

    assert protocol.message_id("9922432407", 1) == hashlib.md5(
        b"9922432407-1"
    ).hexdigest()
    assert protocol.message_id("9922432407", 1) != protocol.message_id("9922432407", 2)


def test_open_barrier_matches_the_only_relay_command_ever_captured():
    envelope = protocol.open_barrier(SERIAL)
    assert envelope.method == METHOD_OUTPUT_CTRL
    assert envelope.task_no == 1
    assert envelope.data == {"relay1Out": [1, 1000], "beepOut": [1, 100]}


def test_barrier_pulse_is_configurable():
    envelope = protocol.open_barrier(SERIAL, pulse_ms=2000, beep_ms=0)
    assert envelope.data == {"relay1Out": [1, 2000]}


def test_signage_is_a_bare_status_object_not_an_envelope():
    assert protocol.signage(protocol.STATUS_WELCOME) == '{"status":"welcome"}'


def test_print_ticket_uses_task_no_2():
    envelope = protocol.print_ticket(SERIAL, [{"uartNo": 2}], "abc")
    assert envelope.task_no == 2
    assert envelope.method == METHOD_TX_UART_DATA


def test_input_info_reports_sensor_state():
    envelope = protocol.input_info(SERIAL, input3=1, input2=1)
    assert envelope.method == METHOD_INPUT_INFO
    assert envelope.get("input3") == 1


def test_ack_carries_empty_data_like_the_real_board():
    envelope = protocol.ack(SERIAL, METHOD_TX_UART_DATA)
    assert envelope.method == METHOD_TX_UART_DATA
    assert envelope.serial_no == SERIAL
    assert envelope.data == {}
    assert envelope.to_json().endswith('"method":"txUartData","data":{}}')


def test_controller_status_reports_inputs_relays_and_beep():
    envelope = protocol.controller_status(
        SERIAL,
        inputs={"input3": 1},
        relays={"relay1": 1},
        beep=1,
    )
    assert envelope.method == "status"
    assert envelope.data == {
        "input1": 0,
        "input2": 0,
        "input3": 1,
        "input4": 0,
        "relay1": 1,
        "relay2": 0,
        "relay3": 0,
        "beep": 1,
    }


def test_controller_status_defaults_to_all_zeros():
    envelope = protocol.controller_status(SERIAL)
    assert envelope.data == {
        "input1": 0,
        "input2": 0,
        "input3": 0,
        "input4": 0,
        "relay1": 0,
        "relay2": 0,
        "relay3": 0,
        "beep": 0,
    }
