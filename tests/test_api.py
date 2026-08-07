"""HTTP surface tests.

The response shapes here are what the Tauri cashier frontend consumes, so they
are pinned against the bodies captured on the wire (flow.md §6, §9).
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trafix.api import create_app
from trafix.service import ParkingService

PLATE = "H488AI"


@pytest.fixture
def app(session_factory, publisher, clock, tmp_path):
    from trafix.storage import SnapshotStore

    config = SimpleNamespace(
        env="test",
        open_gate_url="http://127.0.0.1:8090/open-gate",
        policies=SimpleNamespace(
            storage_dir=tmp_path / "storage",
            require_plate_match=False,
            command_exit_barrier=True,
        ),
    )
    service = ParkingService(
        session_factory,
        publisher=publisher,
        clock=clock,
        print_gap_seconds=0,
        storage=SnapshotStore(config.policies.storage_dir),
    )
    return create_app(config, service)


@pytest.fixture
def client(app):
    return TestClient(app)


def enter(client, plate=PLATE, vehicle_id=1):
    response = client.post(
        "/api/gatein",
        json={
            "gate": "1",
            "vehicle_id": vehicle_id,
            "plate_num": plate,
            "url_gambar": "",
            "serialNo": "441D6491AF17",
        },
    )
    assert response.status_code == 200
    return response.json()


# -- entry ------------------------------------------------------------------


def test_gatein_returns_the_ticket_code(client):
    body = enter(client)
    assert body["status"] == "success"
    assert len(body["kode_tiket"]) == 10
    assert body["police_number"] == PLATE


def test_gatein_prints(client, publisher):
    enter(client)
    assert len(publisher.printed) == 2


def test_gatein_without_a_plate_still_issues_a_ticket(client):
    body = enter(client, plate="")
    assert body["status"] == "success"
    assert body["kode_tiket"]


# -- the LPR-driven entry routes ---------------------------------------------


def test_lpr_gatein_creates_a_transaction(client):
    response = client.post(
        "/api/lpr/gatein",
        data={"plate_num": PLATE},
        files={"image": ("CAMIN_LPR.jpg", b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert len(body["transaction_code"]) == 10


def test_lpr_gatein_requires_plate_and_image(client):
    no_plate = client.post(
        "/api/lpr/gatein", files={"image": ("a.jpg", b"x", "image/jpeg")}
    )
    assert no_plate.status_code == 400
    assert no_plate.json()["message"] == "Missing image or plate_num"

    no_image = client.post("/api/lpr/gatein", data={"plate_num": PLATE})
    assert no_image.status_code == 400


def test_lpr_gateinimage_attaches_to_an_open_ticket(client, clock):
    ticket = enter(client)
    clock.advance(minutes=5)
    response = client.post(
        "/api/lpr/gateinimage",
        data={"transaction_code": ticket["kode_tiket"], "plate_num": PLATE},
        files={"image": ("CAMIN_LPR_2.jpg", b"\xff\xd8fake", "image/jpeg")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["camin_lpr"].startswith("storage/")
    assert body["transaction_code"] == ticket["kode_tiket"]


def test_lpr_gateinimage_404s_for_an_unknown_ticket(client):
    response = client.post(
        "/api/lpr/gateinimage",
        data={"transaction_code": "0000000000"},
        files={"image": ("a.jpg", b"x", "image/jpeg")},
    )
    assert response.status_code == 404
    assert response.json()["message"] == "Transaction not found"


def test_lpr_checkimage_reports_a_reachable_image(client, clock, monkeypatch):
    enter(client)
    clock.advance(minutes=30)

    class _Probe:
        is_success = True
        status_code = 200
        headers = {"content-type": "image/jpeg"}

    monkeypatch.setattr("trafix.api.httpx.get", lambda url, **kw: _Probe())
    response = client.post(
        "/api/lpr/checkimage",
        data={"plate_num": PLATE, "url_image": "http://lpr/image.jpg"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["message"] == "Image is available"
    assert body["transaction_code"]


def test_lpr_checkimage_rejects_a_non_image_url(client, clock, monkeypatch):
    enter(client)
    clock.advance(minutes=30)

    class _NotImage:
        is_success = True
        status_code = 200
        headers = {"content-type": "text/html"}

    monkeypatch.setattr("trafix.api.httpx.get", lambda url, **kw: _NotImage())
    response = client.post(
        "/api/lpr/checkimage",
        data={"plate_num": PLATE, "url_image": "http://lpr/page.html"},
    )
    assert response.status_code == 400
    assert response.json()["message"] == "URL is reachable but not an image"


def test_lpr_checkimage_404s_without_an_open_session(client):
    response = client.post(
        "/api/lpr/checkimage",
        data={"plate_num": "NOPE", "url_image": "http://lpr/x.jpg"},
    )
    assert response.status_code == 404
    assert response.json()["message"] == "Active transaction not found for this plate_num"


def test_lpr_checkimage_requires_url_and_plate(client):
    response = client.post("/api/lpr/checkimage", data={"plate_num": PLATE})
    assert response.status_code == 400
    assert response.json()["message"] == "Missing url_image or plate_num"


# -- the endpoint that is broken in production ------------------------------


def test_lpr_gateout_works(client, clock, publisher):
    """flow.md §7.1: this route 500s on site because the method is missing.

    Here it settles the transaction and releases the vehicle.
    """
    ticket = enter(client)
    clock.advance(hours=2)

    response = client.post(
        "/api/lpr/gateout",
        json={
            "gate_out": "2",
            "transaction_code": ticket["kode_tiket"],
            "plate_num": PLATE,
            "url_gambar": "",
        },
    )

    assert response.status_code == 200, "production returns 500 here"
    body = response.json()
    assert body["status"] == "success_ticket"
    assert body["total"] > 0
    assert publisher.barriers == [("2", True)]


def test_lpr_gateout_by_plate_alone(client, clock):
    enter(client)
    clock.advance(hours=1)
    response = client.post(
        "/api/lpr/gateout", json={"gate_out": "2", "plate_num": PLATE}
    )
    assert response.json()["status"] == "success_ticket"


def test_lpr_gateout_for_an_unknown_vehicle(client):
    response = client.post(
        "/api/lpr/gateout", json={"gate_out": "2", "plate_num": "ZZ9999ZZ"}
    )
    assert response.json()["status"] == "notfound"


def test_lpr_gateout_refuses_a_used_ticket(client, clock):
    ticket = enter(client)
    clock.advance(hours=1)
    client.post("/api/lpr/gateout", json={"transaction_code": ticket["kode_tiket"]})

    again = client.post(
        "/api/lpr/gateout", json={"transaction_code": ticket["kode_tiket"]}
    )
    assert again.json()["status"] == "ticket_used"


# -- the cashier path -------------------------------------------------------


def test_cashier_page_injects_gate_open_url(client):
    """The desk page is wired to the cashier's local gate-open daemon."""
    html = client.get("/cashier").text
    assert "OPEN_GATE_URL" in html
    assert "http://127.0.0.1:8090/open-gate" in html
    assert "openGate(" in html


def test_cashier_page_defaults_to_the_site_gate_open_url(session_factory, publisher, clock, tmp_path):
    from types import SimpleNamespace

    from trafix.storage import SnapshotStore

    config = SimpleNamespace(
        env="test",
        open_gate_url="",
        policies=SimpleNamespace(
            storage_dir=tmp_path / "storage",
            require_plate_match=False,
            command_exit_barrier=True,
        ),
    )
    service = ParkingService(
        session_factory,
        publisher=publisher,
        clock=clock,
        print_gap_seconds=0,
        storage=SnapshotStore(config.policies.storage_dir),
    )
    app = create_app(config, service)

    html = TestClient(app).get("/cashier").text
    assert "http://192.168.1.2:8090/open-gate" in html


def test_detailtransaction_accepts_multipart(client, clock):
    """The real cashier app posts multipart here."""
    ticket = enter(client)
    clock.advance(hours=2)

    response = client.post(
        "/api/gateout/detailtransaction",
        files={
            "transaction_code": (None, ticket["kode_tiket"]),
            "gate_out": (None, "2"),
            "admin_id": (None, "1"),
            "shift_id": (None, "1"),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["status_code"] == 200
    assert body["data"]["transaction_code"] == ticket["kode_tiket"]
    assert body["data"]["total"] > 0
    assert body["data"]["duration"]


def test_detailtransaction_does_not_settle(client, clock):
    ticket = enter(client)
    clock.advance(hours=1)
    client.post(
        "/api/gateout/detailtransaction",
        data={"transaction_code": ticket["kode_tiket"]},
    )
    # Still chargeable, so it was not checked out.
    second = client.post(
        "/api/gateout/detailtransaction",
        data={"transaction_code": ticket["kode_tiket"]},
    )
    assert second.json()["data"]["total"] > 0


def test_detailtransaction_404s_for_an_unknown_ticket(client):
    response = client.post(
        "/api/gateout/detailtransaction", data={"transaction_code": "0000000000"}
    )
    assert response.status_code == 404
    assert response.json()["status"] == "notfound"


def test_gateoutkasir_accepts_form_encoding_and_settles(client, clock, publisher):
    ticket = enter(client)
    clock.advance(hours=3)

    response = client.put(
        "/api/gateout/gateoutKasir",
        data={
            "transaction_code": ticket["kode_tiket"],
            "gate_out": "2",
            "admin_id": "1",
            "shift_id": "1",
            "discount_card": "",
            "total_discount": "0",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["status_code"] == 200
    assert body["data"]["total"] > 0
    assert publisher.barriers == [("2", True)], "production never opens the exit (§7.6)"


def test_gateoutkasir_reports_an_already_used_ticket(client, clock):
    ticket = enter(client)
    clock.advance(hours=1)
    client.put("/api/gateout/gateoutKasir", data={"transaction_code": ticket["kode_tiket"]})

    again = client.put(
        "/api/gateout/gateoutKasir", data={"transaction_code": ticket["kode_tiket"]}
    )
    assert again.json()["status"] == "already_paid"


def test_member_response_carries_the_member_discriminator(
    client, clock, session_factory
):
    from sqlalchemy import select

    from trafix.models import Members

    with session_factory() as session:
        member = session.scalar(select(Members))
        member.vehicle_id = 1
        plate = member.police_number
        session.commit()

    enter(client, plate=plate)
    clock.advance(hours=4)

    body = client.post(
        "/api/gateout/detailtransaction", data={"police_number": plate}
    ).json()

    assert body["transaction"] == "member"
    assert body["message"] == "success_member"
    assert body["data"]["name"] == "Angelo"
    assert body["data"]["total"] == 0


# -- checkimagegateout ------------------------------------------------------


def test_checkimagegateout_returns_the_laravel_nested_shape(client, clock):
    enter(client)
    clock.advance(minutes=30)
    response = client.post("/api/lpr/checkimagegateout", params={"plate_num": PLATE})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert {"image", "gatein", "gateout"} <= set(body)
    assert body["gatein"]["transaction_code"]
    assert body["gatein"]["police_number"] == PLATE
    assert body["image"] == {"available": False, "message": "No url_image provided", "url_image": None}


def test_checkimagegateout_finds_a_parked_car(client, clock):
    enter(client)
    clock.advance(minutes=30)
    response = client.post("/api/lpr/checkimagegateout", params={"plate_num": PLATE})
    assert response.status_code == 200


def test_checkimagegateout_404s_like_the_real_one(client):
    response = client.post("/api/lpr/checkimagegateout", params={"plate_num": "NOPE"})
    assert response.status_code == 404
    assert "not found" in response.json()["message"].lower()


# -- the automated RFID exit (PUT /api/lpr/gateoutcard) ----------------------


def test_lpr_gateoutcard_resolves_a_member(client, clock):
    entered = client.post(
        "/api/gatein/card",
        json={
            "gate": "1",
            "card_no": "006343040",
            "serialNo": "441D6491AF17",
            "vehicle_id": 1,
        },
    )
    assert entered.status_code == 200

    clock.advance(hours=1)
    response = client.put(
        "/api/lpr/gateoutcard",
        data={
            "card": "006343040",
            "gate_out": "2",
            "plate_num": "H4818AI",
            "url_gambar": "",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "success_member"}


def test_lpr_gateoutcard_resolves_a_ticket(client, clock):
    ticket = enter(client)
    clock.advance(hours=1)
    response = client.put(
        "/api/lpr/gateoutcard",
        data={"card": ticket["kode_tiket"], "gate_out": "2", "plate_num": PLATE},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "success_ticket"}


def test_lpr_gateoutcard_reports_an_unknown_card(client):
    response = client.put(
        "/api/lpr/gateoutcard",
        data={"card": "0000000001", "gate_out": "2", "plate_num": "ZZ9999ZZ"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "failed_member"}


def test_lpr_gateoutcard_refuses_a_used_ticket(client, clock):
    ticket = enter(client)
    clock.advance(hours=1)
    client.put("/api/lpr/gateoutcard", data={"card": ticket["kode_tiket"], "gate_out": "2"})
    again = client.put(
        "/api/lpr/gateoutcard", data={"card": ticket["kode_tiket"], "gate_out": "2"}
    )
    assert again.json() == {"status": "ticket_used"}


# -- CORS, which the Tauri app depends on -----------------------------------


def test_preflight_from_the_tauri_origin_is_allowed(client):
    response = client.options(
        "/api/gateout/gateoutKasir",
        headers={
            "Origin": "tauri://localhost",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert response.status_code in (200, 204)
    assert "access-control-allow-origin" in response.headers


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"
