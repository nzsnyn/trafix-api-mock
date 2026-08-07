"""Configuration for every process in the system.

Addresses come from ``config/devices.yaml``, selected with ``TRAFIX_ENV``:
``sim`` runs everything on localhost, ``site`` uses the real 192.168.1.x
addresses from flow.md §3. No code changes between them.

The config file is looked up in this order, so an installed ``trafix`` command
works from any directory and a site deployment can keep its configuration
outside the source tree:

1. an explicit ``path=`` argument
2. ``$TRAFIX_CONFIG`` — full path to a devices.yaml
3. ``./config/devices.yaml`` relative to the current directory
4. the ``config/`` directory beside this source tree
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

CONFIG_FILENAME = "devices.yaml"
ENV_CONFIG = "TRAFIX_CONFIG"
ENV_STORAGE = "TRAFIX_STORAGE"


class ConfigError(Exception):
    """Raised when the configuration is missing or inconsistent."""


def find_config_file(path: Path | None = None) -> Path:
    """Locate devices.yaml. See the module docstring for the search order."""
    if path is not None:
        resolved = Path(path)
        if not resolved.exists():
            raise ConfigError(f"config file not found: {resolved}")
        return resolved

    override = os.environ.get(ENV_CONFIG)
    if override:
        resolved = Path(override)
        if not resolved.exists():
            raise ConfigError(
                f"${ENV_CONFIG} points at {resolved}, which does not exist"
            )
        return resolved

    candidates = [Path.cwd() / "config" / CONFIG_FILENAME, CONFIG_DIR / CONFIG_FILENAME]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise ConfigError(
        f"no {CONFIG_FILENAME} found. Looked in: "
        + ", ".join(str(candidate) for candidate in candidates)
        + f". Set ${ENV_CONFIG} to point at one."
    )


@dataclass(frozen=True)
class BrokerConfig:
    host: str
    port: int
    keepalive: int
    username: str | None
    password: str | None
    client_id_prefix: str


@dataclass(frozen=True)
class GateControllerConfig:
    """The relay + printer board. ``.204`` on site."""

    name: str
    gate: str
    host: str
    serial_no: str


@dataclass(frozen=True)
class LprConfig:
    """An LPR unit.

    The entry unit answers ``GET :8090/checklpr``. The exit unit publishes to
    MQTT instead and, on site, serves nothing at all — see flow.md §7.2.
    """

    name: str
    gate: str
    host: str
    port: int
    base_url: str
    public_url: str
    serves_http: bool
    # The gate number the device actually uses on the `gate/out/{gate}/pos`
    # wire topic. Decoupled from the logical `gate` because the real exit LPR
    # (.149) publishes with "1" while the exit lane is logically gate "2"
    # (flow.md §8). Defaults to the logical gate.
    pos_topic_gate: str = ""


@dataclass(frozen=True)
class CameraConfig:
    """A Uniview IP camera used for the CCTV snapshot."""

    name: str
    host: str
    snapshot_path: str
    username: str | None
    password: str | None


@dataclass(frozen=True)
class ApiConfig:
    host: str
    port: int
    base_url: str


@dataclass(frozen=True)
class Policies:
    lpr_timeout_seconds: float
    lpr_retries: int
    button_debounce_seconds: float
    barrier_pulse_ms: int
    barrier_beep_ms: int
    # Fixes flow.md §7.6 — production never commands the exit barrier.
    command_exit_barrier: bool
    # Fixes §7.7 — plate is advisory, the ticket code is authoritative.
    require_plate_match: bool
    storage_dir: Path


@dataclass(frozen=True)
class Config:
    env: str
    broker: BrokerConfig
    database_url: str
    api: ApiConfig
    # Where the cashier's local gate-open daemon listens. In production the
    # Tauri app POSTs to http://192.168.1.2:8090/open-gate after settling an
    # exit — that service physically raises the exit barrier.
    open_gate_url: str
    controllers: dict[str, GateControllerConfig]  # keyed by gate
    lpr: dict[str, LprConfig]  # keyed by gate
    cameras: dict[str, CameraConfig]  # keyed by name
    policies: Policies

    def controller_for(self, gate: str) -> GateControllerConfig:
        try:
            return self.controllers[str(gate)]
        except KeyError:
            raise ConfigError(f"no gate controller configured for gate {gate!r}")

    def lpr_for(self, gate: str) -> LprConfig:
        try:
            return self.lpr[str(gate)]
        except KeyError:
            raise ConfigError(f"no LPR configured for gate {gate!r}")


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


def _env(value: Any) -> Any:
    """Expand ``${VAR}`` and ``${VAR:-default}`` from the environment."""
    if not isinstance(value, str) or not value.startswith("${"):
        return value
    body = value[2:-1]
    name, _, default = body.partition(":-")
    return os.environ.get(name, default)


def load_config(env: str | None = None, path: Path | None = None) -> Config:
    path = find_config_file(path)
    raw = _read_yaml(path)
    environments = _require(raw, "environments", str(path))

    env = env or os.environ.get("TRAFIX_ENV") or raw.get("default_env")
    if not env:
        raise ConfigError("no environment selected and no default_env set")
    if env not in environments:
        raise ConfigError(
            f"unknown environment {env!r}; available: {sorted(environments)}"
        )

    block = environments[env]
    where = f"environments.{env}"

    broker_raw = _require(block, "broker", where)
    broker = BrokerConfig(
        host=str(_env(_require(broker_raw, "host", f"{where}.broker"))),
        port=int(_env(broker_raw.get("port", 1883))),
        keepalive=int(broker_raw.get("keepalive", 30)),
        username=_env(broker_raw.get("username")) or None,
        password=_env(broker_raw.get("password")) or None,
        client_id_prefix=str(broker_raw.get("client_id_prefix", "trafix")),
    )

    api_raw = block.get("api", {})
    api = ApiConfig(
        host=str(_env(api_raw.get("host", "127.0.0.1"))),
        port=int(_env(api_raw.get("port", 8000))),
        base_url=str(
            _env(api_raw.get("base_url"))
            or f"http://{api_raw.get('host', '127.0.0.1')}:{api_raw.get('port', 8000)}"
        ).rstrip("/"),
    )

    controllers: dict[str, GateControllerConfig] = {}
    for name, entry in (block.get("controllers") or {}).items():
        gate = str(_require(entry, "gate", f"{where}.controllers.{name}"))
        controllers[gate] = GateControllerConfig(
            name=name,
            gate=gate,
            host=str(_env(_require(entry, "host", f"{where}.controllers.{name}"))),
            serial_no=str(_env(entry.get("serial_no")) or ""),
        )

    lpr: dict[str, LprConfig] = {}
    for name, entry in (block.get("lpr") or {}).items():
        gate = str(_require(entry, "gate", f"{where}.lpr.{name}"))
        host = str(_env(_require(entry, "host", f"{where}.lpr.{name}")))
        port = int(_env(entry.get("port", 8090)))
        base_url = str(_env(entry.get("base_url")) or f"http://{host}:{port}")
        lpr[gate] = LprConfig(
            name=name,
            gate=gate,
            host=host,
            port=port,
            base_url=base_url.rstrip("/"),
            public_url=str(_env(entry.get("public_url")) or base_url).rstrip("/"),
            serves_http=bool(entry.get("serves_http", True)),
            pos_topic_gate=str(_env(entry.get("pos_topic_gate")) or gate),
        )

    cameras: dict[str, CameraConfig] = {}
    for name, entry in (block.get("cameras") or {}).items():
        cameras[name] = CameraConfig(
            name=name,
            host=str(_env(_require(entry, "host", f"{where}.cameras.{name}"))),
            snapshot_path=str(entry.get("snapshot_path", "/cgi-bin/snapshot.cgi")),
            username=_env(entry.get("username")) or None,
            password=_env(entry.get("password")) or None,
        )

    policies = _parse_policies(raw.get("policies", {}), config_path=path)
    database_url = str(_env(_require(block, "database_url", where)))
    open_gate_url = str(_env(block.get("open_gate_url")) or "")

    return Config(
        env=env,
        broker=broker,
        database_url=database_url,
        api=api,
        open_gate_url=open_gate_url,
        controllers=controllers,
        lpr=lpr,
        cameras=cameras,
        policies=policies,
    )


def _parse_policies(raw: dict[str, Any], *, config_path: Path | None = None) -> Policies:
    storage = _resolve_storage(raw.get("storage_dir", "storage"), config_path)
    return Policies(
        lpr_timeout_seconds=float(raw.get("lpr_timeout_seconds", 5.0)),
        lpr_retries=int(raw.get("lpr_retries", 1)),
        button_debounce_seconds=float(raw.get("button_debounce_seconds", 5.0)),
        barrier_pulse_ms=int(raw.get("barrier_pulse_ms", 1000)),
        barrier_beep_ms=int(raw.get("barrier_beep_ms", 100)),
        command_exit_barrier=bool(raw.get("command_exit_barrier", True)),
        require_plate_match=bool(raw.get("require_plate_match", False)),
        storage_dir=storage,
    )


def _resolve_storage(value: Any, config_path: Path | None) -> Path:
    """Where snapshots are written.

    ``$TRAFIX_STORAGE`` wins. Otherwise a relative ``storage_dir`` resolves
    against the directory containing ``config/``, so a deployment that keeps its
    configuration outside the source tree keeps its images beside it too.
    """
    override = os.environ.get(ENV_STORAGE)
    if override:
        return Path(override).expanduser().resolve()

    storage = Path(str(value)).expanduser()
    if storage.is_absolute():
        return storage

    if config_path is not None:
        # config_path is <base>/config/devices.yaml -> base
        base = config_path.resolve().parent.parent
    else:
        base = PROJECT_ROOT
    return base / storage
