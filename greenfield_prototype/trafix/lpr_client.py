"""HTTP client for the LPR cameras. The only module that talks to them.

A capture never raises at the caller: a dead camera or an unreadable plate both
come back as a :class:`PlateRead` with ``ok=False`` and a reason, because the
server has to make a gate decision either way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from trafix.config import LprConfig, Policies
from trafix.tickets import normalize_plate

log = logging.getLogger(__name__)

CAPTURE_PATH = "/api/v1/capture"
HEALTH_PATH = "/api/v1/health"

# Why a read failed. The server maps these onto its lpr_failure policy.
REASON_UNREACHABLE = "unreachable"
REASON_HTTP_ERROR = "http_error"
REASON_BAD_RESPONSE = "bad_response"
REASON_NO_PLATE = "no_plate"
REASON_LOW_CONFIDENCE = "low_confidence"


@dataclass(frozen=True)
class PlateRead:
    ok: bool
    plate: str | None = None
    confidence: float | None = None
    image_url: str | None = None
    captured_at: str | None = None
    reason: str | None = None
    detail: str | None = None

    def as_detail(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "plate": self.plate,
            "confidence": self.confidence,
            "image_url": self.image_url,
            "reason": self.reason,
            "detail": self.detail,
        }


class LprClient:
    """Talks to one camera."""

    def __init__(self, config: LprConfig, policies: Policies) -> None:
        self.config = config
        self.policies = policies
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=policies.lpr_timeout_seconds,
            headers={"User-Agent": "trafix-server/1.0"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LprClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def health(self) -> bool:
        try:
            response = self._client.get(HEALTH_PATH)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def capture(self, *, trigger: str = "gate", lane: str | None = None) -> PlateRead:
        """Ask the camera to read the plate in front of it.

        Retries ``lpr_retries`` times on transport failure; a camera that
        answers "no plate" is a real answer and is not retried.
        """
        body = {"trigger": trigger, "lane": lane or self.config.lane}
        attempts = max(1, self.policies.lpr_retries + 1)
        last: PlateRead | None = None

        for attempt in range(1, attempts + 1):
            last = self._capture_once(body)
            if last.ok or last.reason not in (REASON_UNREACHABLE, REASON_HTTP_ERROR):
                return last
            log.warning(
                "%s capture attempt %s/%s failed: %s",
                self.config.name,
                attempt,
                attempts,
                last.detail,
            )

        assert last is not None
        return last

    def _capture_once(self, body: dict[str, Any]) -> PlateRead:
        try:
            response = self._client.post(CAPTURE_PATH, json=body)
        except httpx.HTTPError as exc:
            return PlateRead(
                ok=False, reason=REASON_UNREACHABLE, detail=f"{type(exc).__name__}: {exc}"
            )

        if response.status_code >= 400:
            return PlateRead(
                ok=False,
                reason=REASON_HTTP_ERROR,
                detail=f"HTTP {response.status_code}",
            )

        try:
            data = response.json()
        except ValueError as exc:
            return PlateRead(ok=False, reason=REASON_BAD_RESPONSE, detail=str(exc))

        if not isinstance(data, dict):
            return PlateRead(
                ok=False, reason=REASON_BAD_RESPONSE, detail="response is not an object"
            )

        plate = normalize_plate(data.get("plate"))
        confidence = _as_float(data.get("confidence"))
        image_url = data.get("image_url")
        captured_at = data.get("captured_at")

        if not plate:
            return PlateRead(
                ok=False,
                reason=REASON_NO_PLATE,
                confidence=confidence,
                image_url=image_url,
                captured_at=captured_at,
                detail=str(data.get("message") or "camera returned no plate"),
            )

        if confidence is not None and confidence < self.policies.lpr_min_confidence:
            return PlateRead(
                ok=False,
                reason=REASON_LOW_CONFIDENCE,
                plate=plate,
                confidence=confidence,
                image_url=image_url,
                captured_at=captured_at,
                detail=f"confidence {confidence:.2f} below "
                f"{self.policies.lpr_min_confidence:.2f}",
            )

        return PlateRead(
            ok=True,
            plate=plate,
            confidence=confidence,
            image_url=image_url,
            captured_at=captured_at,
        )


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
