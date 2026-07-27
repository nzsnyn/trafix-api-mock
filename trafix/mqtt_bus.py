"""MQTT transport. The only module in the project that imports paho.

Both the orchestrator and the device mocks use this, so reconnection, auth and
envelope decoding are defined once.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Callable

import paho.mqtt.client as mqtt

from trafix.config import BrokerConfig
from trafix.protocol import Envelope, ProtocolError, parse

log = logging.getLogger(__name__)

EnvelopeHandler = Callable[[str, Envelope], None]
RawHandler = Callable[[str, str], None]


class MqttBus:
    """A connected MQTT client.

    Subscriptions may be registered before :meth:`connect` and are re-applied
    on every reconnect, so a broker restart does not silently deafen a device.
    """

    def __init__(
        self,
        broker: BrokerConfig,
        client_id: str,
        *,
        will_topic: str | None = None,
        will_payload: str | None = None,
    ) -> None:
        self.broker = broker
        self.client_id = f"{broker.client_id_prefix}-{client_id}"
        self._handlers: dict[str, list[EnvelopeHandler]] = {}
        self._raw_handlers: dict[str, list[RawHandler]] = {}
        self._connected = threading.Event()

        # A unique suffix matters here: flow.md §8 records that the Laravel app
        # uses a fixed client_id with clean_session=false, so two publishers
        # sharing an id evict each other. Never collide with it.
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{self.client_id}-{uuid.uuid4().hex[:6]}",
            clean_session=True,
        )
        if broker.username:
            self._client.username_pw_set(broker.username, broker.password or None)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=15)

        if will_topic:
            self._client.will_set(will_topic, will_payload or "offline", qos=1, retain=True)

    # -- lifecycle ---------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> None:
        log.info("connecting to broker %s:%s", self.broker.host, self.broker.port)
        self._client.connect(self.broker.host, self.broker.port, self.broker.keepalive)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            self._client.loop_stop()
            raise ConnectionError(
                f"broker {self.broker.host}:{self.broker.port} did not respond "
                f"within {timeout}s"
            )

    def disconnect(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()
        self._connected.clear()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def __enter__(self) -> "MqttBus":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.disconnect()

    # -- pub / sub ---------------------------------------------------------

    def subscribe(self, topic: str, handler: EnvelopeHandler) -> None:
        """Handle decoded envelopes on ``topic``."""
        first = self._is_new_topic(topic)
        self._handlers.setdefault(topic, []).append(handler)
        if first and self.is_connected:
            self._client.subscribe(topic, qos=1)

    def subscribe_raw(self, topic: str, handler: RawHandler) -> None:
        """Handle the payload as a plain string.

        The signage topic carries a bare ``{"status":"welcome"}``, not an
        envelope, so it cannot go through :meth:`subscribe`.
        """
        first = self._is_new_topic(topic)
        self._raw_handlers.setdefault(topic, []).append(handler)
        if first and self.is_connected:
            self._client.subscribe(topic, qos=1)

    def publish(self, topic: str, message: Envelope, *, retain: bool = False) -> None:
        self.publish_raw(topic, message.to_json(), retain=retain)

    def publish_raw(self, topic: str, payload: str, *, retain: bool = False) -> None:
        log.debug("-> %s %s", topic, payload)
        info = self._client.publish(topic, payload, qos=1, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.error("publish to %s failed with rc=%s", topic, info.rc)

    def _is_new_topic(self, topic: str) -> bool:
        return topic not in self._handlers and topic not in self._raw_handlers

    # -- callbacks ---------------------------------------------------------

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties=None):
        if reason_code != 0:
            log.error("broker refused connection: %s", reason_code)
            return
        log.info("connected as %s", self.client_id)
        for topic in {*self._handlers, *self._raw_handlers}:
            self._client.subscribe(topic, qos=1)
        self._connected.set()

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties=None):
        self._connected.clear()
        if reason_code != 0:
            log.warning("lost broker connection (%s), retrying", reason_code)

    def _on_message(self, _client, _userdata, message):
        raw_handlers = self._raw_handlers.get(message.topic, [])
        if raw_handlers:
            text = message.payload.decode("utf-8", errors="replace")
            for handler in raw_handlers:
                self._safely(handler, message.topic, text)

        envelope_handlers = self._handlers.get(message.topic, [])
        if not envelope_handlers:
            return

        try:
            envelope = parse(message.payload)
        except ProtocolError as exc:
            log.warning("dropping malformed message on %s: %s", message.topic, exc)
            return

        for handler in envelope_handlers:
            self._safely(handler, message.topic, envelope)

    @staticmethod
    def _safely(handler, topic: str, payload) -> None:
        """A failing handler must not kill the network loop."""
        try:
            handler(topic, payload)
        except Exception:
            log.exception("handler for %s failed", topic)
