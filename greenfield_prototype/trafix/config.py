"""Load and validate the device / tariff configuration.

Everything that knows an IP address or a port reads it from here. Switching
between the local simulator and the real site is a matter of TRAFIX_ENV, never
a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from trafix.envelope import DEFAULT_TOPIC_ROOT, set_topic_root

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

LANES = ("in", "out")


class ConfigError(Exception):
    """Raised when the configuration files are missing or inconsistent."""


@dataclass(frozen=True)
class BrokerConfig:
    host: str
    port: int
    keepalive: int
    client_id_prefix: str
    topic_root: str


@dataclass(frozen=True)
class LprConfig:
    """One LPR camera. ``base_url`` is how the server reaches it."""

    name: str
    lane: str
    host: str
    port: int
    base_url: str
    public_url: str


@dataclass(frozen=True)
class ManlessConfig:
    """One manless terminal. It is reached over MQTT, so it has no port."""

    name: str
    lane: str
    host: str


@dataclass(frozen=True)
class Policies:
    lpr_failure: str
    plate_mismatch: str
    button_debounce_seconds: float
    lpr_timeout_seconds: float
    lpr_retries: int
    lpr_min_confidence: float


@dataclass(frozen=True)
class Tariff:
    currency: str
    grace_minutes: int
    first_hour: int
    next_hour: int
    daily_max: int | None
    lost_ticket: int
    rounding: int


@dataclass(frozen=True)
class Config:
    env: str
    broker: BrokerConfig
    manless: dict[str, ManlessConfig]  # keyed by lane
    lpr: dict[str, LprConfig]  # keyed by lane
    policies: Policies
    tariff: Tariff
    database: Path

    def manless_for(self, lane: str) -> ManlessConfig:
        return self.manless[_check_lane(lane)]

    def lpr_for(self, lane: str) -> LprConfig:
        return self.lpr[_check_lane(lane)]


def _check_lane(lane: str) -> str:
    if lane not in LANES:
        raise ConfigError(f"unknown lane {lane!r}, expected one of {LANES}")
    return lane


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"missing {key!r} in {where}")
    return mapping[key]


def _one_of(value: Any, allowed: tuple[str, ...], where: str) -> str:
    if value not in allowed:
        raise ConfigError(f"{where} must be one of {allowed}, got {value!r}")
    return str(value)


def load_config(
    env: str | None = None,
    devices_path: Path | None = None,
    tariff_path: Path | None = None,
) -> Config:
    """Build a :class:`Config` from the YAML files.

    ``env`` falls back to ``$TRAFIX_ENV`` and then to ``default_env``.
    """

    devices_path = devices_path or CONFIG_DIR / "devices.yaml"
    tariff_path = tariff_path or CONFIG_DIR / "tariff.yaml"

    raw = _read_yaml(devices_path)
    environments = _require(raw, "environments", str(devices_path))

    env = env or os.environ.get("TRAFIX_ENV") or raw.get("default_env")
    if not env:
        raise ConfigError("no environment selected and no default_env set")
    if env not in environments:
        raise ConfigError(
            f"unknown environment {env!r}; available: {sorted(environments)}"
        )

    block = environments[env]
    broker = _parse_broker(_require(block, "broker", f"environments.{env}"))
    manless, lpr = _parse_devices(_require(block, "devices", f"environments.{env}"), env)

    policies = _parse_policies(raw.get("policies", {}))
    tariff = _parse_tariff(_read_yaml(tariff_path))

    # A per-environment database keeps the test suite from writing into the
    # simulator's records, and the simulator out of production's.
    default_database = raw.get("storage", {}).get("database", "trafix.db")
    database = Path(block.get("database", default_database))
    if not database.is_absolute():
        database = PROJECT_ROOT / database

    # Every process is one environment; fix the topic namespace here so no
    # caller has to remember to pass it.
    set_topic_root(broker.topic_root)

    return Config(
        env=env,
        broker=broker,
        manless=manless,
        lpr=lpr,
        policies=policies,
        tariff=tariff,
        database=database,
    )


def _parse_broker(raw: dict[str, Any]) -> BrokerConfig:
    return BrokerConfig(
        host=str(_require(raw, "host", "broker")),
        port=int(raw.get("port", 1883)),
        keepalive=int(raw.get("keepalive", 30)),
        client_id_prefix=str(raw.get("client_id_prefix", "trafix")),
        topic_root=str(raw.get("topic_root", DEFAULT_TOPIC_ROOT)).strip("/"),
    )


def _parse_devices(
    raw: dict[str, Any], env: str
) -> tuple[dict[str, ManlessConfig], dict[str, LprConfig]]:
    manless: dict[str, ManlessConfig] = {}
    lpr: dict[str, LprConfig] = {}

    for name, entry in raw.items():
        if name == "server":
            continue
        where = f"environments.{env}.devices.{name}"
        lane = _one_of(_require(entry, "lane", where), LANES, f"{where}.lane")
        host = str(_require(entry, "host", where))

        if name.startswith("manless"):
            manless[lane] = ManlessConfig(name=name, lane=lane, host=host)
        elif name.startswith("lpr"):
            port = int(entry.get("port", 80))
            base_url = str(entry.get("base_url") or f"http://{host}:{port}")
            lpr[lane] = LprConfig(
                name=name,
                lane=lane,
                host=host,
                port=port,
                base_url=base_url.rstrip("/"),
                public_url=str(entry.get("public_url", base_url)).rstrip("/"),
            )
        else:
            raise ConfigError(f"{where}: device name must start with manless or lpr")

    for lane in LANES:
        if lane not in manless:
            raise ConfigError(f"environment {env!r} has no manless terminal for lane {lane!r}")
        if lane not in lpr:
            raise ConfigError(f"environment {env!r} has no LPR camera for lane {lane!r}")

    return manless, lpr


def _parse_policies(raw: dict[str, Any]) -> Policies:
    return Policies(
        lpr_failure=_one_of(
            raw.get("lpr_failure", "allow"), ("allow", "deny"), "policies.lpr_failure"
        ),
        plate_mismatch=_one_of(
            raw.get("plate_mismatch", "flag"),
            ("allow", "flag", "deny"),
            "policies.plate_mismatch",
        ),
        button_debounce_seconds=float(raw.get("button_debounce_seconds", 5)),
        lpr_timeout_seconds=float(raw.get("lpr_timeout_seconds", 4.0)),
        lpr_retries=int(raw.get("lpr_retries", 1)),
        lpr_min_confidence=float(raw.get("lpr_min_confidence", 0.55)),
    )


def _parse_tariff(raw: dict[str, Any]) -> Tariff:
    daily_max = raw.get("daily_max")
    return Tariff(
        currency=str(raw.get("currency", "IDR")),
        grace_minutes=int(raw.get("grace_minutes", 0)),
        first_hour=int(_require(raw, "first_hour", "tariff")),
        next_hour=int(_require(raw, "next_hour", "tariff")),
        daily_max=None if daily_max is None else int(daily_max),
        lost_ticket=int(raw.get("lost_ticket", 0)),
        rounding=int(raw.get("rounding", 0)),
    )
