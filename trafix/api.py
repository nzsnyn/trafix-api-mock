"""HTTP API — the Laravel application's replacement.

Route names, request fields and response shapes follow
``mqtt_app_lpr/routes/api.php`` and the responses captured on the wire
(flow.md §6, §9), so the existing Tauri cashier frontend keeps working against
this server unchanged.

The one behavioural difference is the point of the exercise:
``POST /api/lpr/gateout`` is implemented here. In production that route points
at a method that does not exist and returns 500 on every automated exit.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from trafix import db, service
from trafix.config import Config
from trafix.service import ParkingService

log = logging.getLogger("api")

router = APIRouter(prefix="/api")

# The cashier desk page and the root redirect live OUTSIDE the /api prefix so
# the desk browses to http://<server>:8000/cashier, not /api/cashier.
web_router = APIRouter()


def _service(request: Request) -> ParkingService:
    return request.app.state.service


def _json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status_code)


CASHIER_PAGE_HTML = """<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kasir Parkir</title>
<style>
  :root { --ink:#1f2430; --muted:#6b7280; --line:#e5e7eb; --ok:#16a34a; --warn:#d97706; --err:#dc2626; --bg:#f3f4f6; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background:var(--bg); color:var(--ink); }
  .wrap { max-width:560px; margin:0 auto; padding:24px 16px 64px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 20px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px; }
  label { display:block; font-size:12px; font-weight:600; margin:10px 0 4px; color:var(--muted); }
  input, select {
    width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:8px;
    font-size:16px; background:#fff; color:var(--ink);
  }
  input:focus { outline:2px solid #2563eb; border-color:transparent; }
  .row { display:flex; gap:10px; }
  .row > div { flex:1; }
  .btns { display:flex; gap:10px; margin-top:16px; }
  button {
    flex:1; padding:12px; border:none; border-radius:8px; font-size:15px; font-weight:600; cursor:pointer;
  }
  .lookup { background:#eef2ff; color:#3730a3; }
  .settle { background:#16a34a; color:#fff; }
  .lost   { background:#fff7ed; color:#9a3412; border:1px solid #fdba74; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .result { white-space:pre-wrap; font-size:14px; line-height:1.6; }
  .result .ok  { color:var(--ok); font-weight:700; }
  .result .err { color:var(--err); font-weight:700; }
  .result .warn{ color:var(--warn); font-weight:700; }
  .k { color:var(--muted); }
  .total { font-size:24px; font-weight:800; margin-top:6px; }
  .note { font-size:12px; color:var(--muted); margin-top:12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Kasir Parkir</h1>
  <p class="sub">Cek tarif, terima pembayaran tunai, lalu buka gate keluar.</p>

  <div class="card">
    <label for="ticket">Nomor tiket / isi QR</label>
    <input id="ticket" autocomplete="off" autofocus placeholder="contoh: 9922432407">

    <label for="plate">Plat nomor (untuk tiket hilang)</label>
    <input id="plate" autocomplete="off" placeholder="contoh: H 488 AI">

    <div class="row">
      <div>
        <label for="gate">Gate keluar</label>
        <input id="gate" value="2" inputmode="numeric">
      </div>
      <div>
        <label for="admin">Admin ID</label>
        <input id="admin" value="1" inputmode="numeric">
      </div>
      <div>
        <label for="shift">Shift ID</label>
        <input id="shift" value="1" inputmode="numeric">
      </div>
    </div>

    <div class="btns">
      <button class="lookup" id="btnLookup">Cek Tarif</button>
      <button class="settle" id="btnSettle">Bayar &amp; Buka Gate</button>
      <button class="lost" id="btnLost">Tiket Hilang</button>
    </div>
  </div>

  <div class="card">
    <div class="result" id="result">Hasil akan tampil di sini.</div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const OPEN_GATE_URL = __OPEN_GATE_URL__;

function money(v) {
  const n = Number(v) || 0;
  return "Rp" + n.toLocaleString("id-ID");
}

function fmt(p) {
  const d = p.data || {};
  const lines = [];
  if (p.status !== "success") {
    lines.push(p.status === "notfound" ? "❌ Transaksi tidak ditemukan." : "❌ " + (p.message || p.status));
    return lines.join("\\n");
  }
  lines.push("✅ Transaksi ditemukan.");
  if (d.name) lines.push("Member: " + d.name);
  if (d.transaction_code) lines.push("Tiket   : " + d.transaction_code);
  if (d.police_number) lines.push("Plat    : " + d.police_number);
  if (d.time_checkin) lines.push("Masuk   : " + d.time_checkin);
  if (d.time_checkout) lines.push("Keluar  : " + d.time_checkout);
  if (d.duration) lines.push("Durasi  : " + d.duration);
  if (d.payment_status) lines.push("Status  : " + d.payment_status);
  if (d.breakdown) lines.push("Rincian : " + d.breakdown);
  if (d.total !== undefined && d.total !== null) lines.push("Total   : " + money(d.total));
  if (p.settled) {
    lines.push("");
    lines.push(p.settled === "already_paid" ? "⚠ Tiket sudah pernah digunakan." : "🔓 Pembayaran diterima — gate keluar dibuka.");
  }
  return lines.join("\\n");
}

async function call(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  let payload;
  try { payload = await res.json(); }
  catch (e) { throw new Error("Respons bukan JSON (" + res.status + ")"); }
  if (res.status === 404 && payload.status === "notfound") return payload;
  if (res.status >= 400 && payload.status !== "notfound") throw new Error(payload.message || res.status);
  return payload;
}

function body() {
  return {
    transaction_code: $("ticket").value.trim(),
    police_number: $("plate").value.trim(),
    gate_out: $("gate").value.trim() || "2",
    admin_id: $("admin").value.trim() || "1",
    shift_id: $("shift").value.trim() || "1",
  };
}

function setBusy(on) {
  $("btnLookup").disabled = on;
  $("btnSettle").disabled = on;
  $("btnLost").disabled = on;
}

$("btnLookup").onclick = async () => {
  $("result").textContent = "Mencari…";
  setBusy(true);
  try {
    const p = await call("POST", "/api/gateout/detailtransaction", body());
    $("result").textContent = fmt(p);
  } catch (e) {
    $("result").textContent = "❌ " + e.message;
  } finally { setBusy(false); }
};

async function settle(lost) {
  $("result").textContent = "Memproses…";
  setBusy(true);
  try {
    const b = body();
    b.lost_ticket = lost ? "1" : "";
    const p = await call("PUT", "/api/gateout/gateoutKasir", b);
    const settled = p.status === "already_paid" ? "already_paid" : (p.status === "success" ? "ok" : null);
    if (!settled) {
      $("result").textContent = "❌ " + (p.message || p.status);
      return;
    }
    let text = fmt({...p, settled});
    if (OPEN_GATE_URL) {
      let gateMsg;
      try {
        gateMsg = await openGate(b);
      } catch (e) {
        gateMsg = "gagal: " + e.message;
      }
      if (gateMsg !== "ok") text += "\n⚠ Gate keluar: " + gateMsg;
    }
    $("result").textContent = text;
  } catch (e) {
    $("result").textContent = "❌ " + e.message;
  } finally { setBusy(false); }
}

async function openGate(b) {
  const fd = new URLSearchParams();
  fd.set("police_number", b.police_number || "");
  fd.set("lpr_plate", b.police_number || "");
  fd.set("transaction_code", b.transaction_code || "");
  const res = await fetch(OPEN_GATE_URL, {
    method: "POST",
        headers: {
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    body: fd.toString(),
  });
  const bodyText = await res.text();
  if (!res.ok) return "HTTP " + res.status + (bodyText ? ": " + bodyText : "");
  return "ok";
}

$("btnSettle").onclick = () => settle(false);
$("btnLost").onclick = () => settle(true);

$("ticket").addEventListener("keydown", e => { if (e.key === "Enter") $("btnSettle").click(); });
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


@router.post("/gatein")
async def gatein(request: Request) -> JSONResponse:
    """Issue a ticket and print it.

    Called by the orchestrator over loopback, which is why it never appears in
    a capture of the external interface (flow.md §3).
    """
    body = await _body(request)
    result = _service(request).gate_in(
        gate=str(body.get("gate", "1")),
        vehicle_id=_int(body.get("vehicle_id")),
        plate_num=body.get("plate_num"),
        url_gambar=body.get("url_gambar"),
        serial_no=str(body.get("serialNo") or body.get("serial_no") or ""),
        ipcam=body.get("ipcam"),
    )
    return _json(
        {
            "status": result.status,
            "kode_tiket": result.transaction_code,
            "police_number": result.plate,
            "typeqr": result.type_qr,
        }
    )


@router.post("/gatein/card")
async def gatein_card(request: Request) -> JSONResponse:
    """Member auto-entry: an RFID ``readCard`` tag resolved to a member.

    Called by the orchestrator over loopback. No ticket is printed — the
    member's subscription covers the stay.
    """
    body = await _body(request)
    result = _service(request).member_gate_in(
        gate=str(body.get("gate", "1")),
        card_no=str(
            body.get("card_no") or body.get("cardNo") or body.get("card") or ""
        ),
        serial_no=str(body.get("serialNo") or body.get("serial_no") or ""),
        vehicle_id=_int(body.get("vehicle_id")),
    )
    if result.status == service.STATUS_NOT_FOUND:
        return _json(
            {"status": "member_notfound", "message": result.message},
            status_code=404,
        )
    if result.status == service.STATUS_MEMBER_EXPIRED:
        return _json(
            {
                "status": "member_expired",
                "message": result.message,
                "name": result.member_name,
            },
            status_code=403,
        )
    return _json(
        {
            "status": result.status,
            "kode_tiket": result.transaction_code,
            "member_code": result.member_code,
            "name": result.member_name,
            "police_number": result.plate,
        }
    )


# ---------------------------------------------------------------------------
# Entry — the LPR unit drives it directly (multipart image uploads)
# ---------------------------------------------------------------------------


@router.post("/lpr/gatein")
async def lpr_gatein(request: Request) -> JSONResponse:
    """The entry LPR unit opens a session from its own read.

    Port of ``GateController::GateInLpr``: the unit uploads its photo and the
    plate it read; a transaction is created and nothing else happens.
    """
    body, image = await _body_and_file(request)
    plate = body.get("plate_num")
    if image is None or not plate:
        return _json(
            {"status": "error", "message": "Missing image or plate_num"},
            status_code=400,
        )
    result = _service(request).lpr_gate_in(plate=plate, image=image)
    return _json(
        {"status": result.status, "transaction_code": result.transaction_code}
    )


@router.post("/lpr/gateinimage")
async def lpr_gateinimage(request: Request) -> JSONResponse:
    """Attach the LPR photo to an open session.

    Port of ``GateController::GateinImageLpr``: the session is found by its
    ticket code or member card, then the photo and plate read are recorded.
    """
    body, image = await _body_and_file(request)
    trxcode = body.get("transaction_code")
    if image is None or not trxcode:
        return _json(
            {"status": "error", "message": "Missing image or transaction_code"},
            status_code=400,
        )
    result = _service(request).attach_gatein_image(
        transaction_code=str(trxcode),
        plate=body.get("plate_num"),
        image=image,
    )
    if result["status"] == service.STATUS_NOT_FOUND:
        return _json(
            {"status": "error", "message": result["message"]}, status_code=404
        )
    return _json(result)


@router.post("/lpr/checkimage")
async def lpr_checkimage(request: Request) -> JSONResponse:
    """Check the entry LPR's photo is fetchable for an open session.

    Port of ``GateController::checkLprImage``. The probe is a real network
    call, so it lives here rather than in the service layer.
    """
    body = await _body(request)
    plate = body.get("plate_num")
    url_image = body.get("url_image")
    if not url_image or not plate:
        return _json(
            {"status": "error", "message": "Missing url_image or plate_num"},
            status_code=400,
        )

    srv = _service(request)
    transaction_code = srv.find_open_plate_code(plate=plate)
    if transaction_code is None:
        return _json(
            {
                "status": "error",
                "message": "Active transaction not found for this plate_num",
                "plate_num": plate,
            },
            status_code=404,
        )

    try:
        probe = httpx.get(url_image, timeout=5, follow_redirects=True)
    except httpx.HTTPError as exc:
        return _json(
            {"status": "error", "message": f"Error checking image: {exc}"},
            status_code=500,
        )
    if not probe.is_success:
        return _json(
            {
                "status": "error",
                "message": "Image is not available or unreachable",
                "status_code": probe.status_code,
            },
            status_code=404,
        )
    content_type = probe.headers.get("content-type", "")
    if "image" not in content_type:
        return _json(
            {
                "status": "error",
                "message": "URL is reachable but not an image",
                "content_type": content_type,
            },
            status_code=400,
        )
    if srv.storage is not None:
        srv.storage.download_async(
            url_image, "lpr/gatein", srv.storage.lpr_filename(url_image)
        )
    return _json(
        {
            "status": "success",
            "message": "Image is available",
            "plate_num": plate,
            "transaction_code": transaction_code,
            "url_image": url_image,
        }
    )


# ---------------------------------------------------------------------------
# Exit — the automated LPR path
# ---------------------------------------------------------------------------


@router.post("/lpr/gateout")
async def lpr_gateout(request: Request) -> JSONResponse:
    """Automated exit by plate or ticket.

    **This is the fix for flow.md §7.1.** ``routes/api.php:205`` maps this path
    to ``GateoutController::GateOutLpr``, a method that was never written, so
    the live site returns::

        Method App\\Http\\Controllers\\GateoutController::GateOutLpr does not exist.

    once per exit event. Modelled on ``GateOutRfidLpr`` (:1603), which is the
    closest working template.
    """
    body = await _body(request)
    result = _service(request).gate_out(
        gate=str(body.get("gate_out") or body.get("gate") or "2"),
        code=body.get("transaction_code") or body.get("card"),
        plate_num=body.get("plate_num"),
        url_gambar=body.get("url_gambar"),
        admin_id=_int(body.get("admin_id")),
        shift_id=_int(body.get("shift_id")),
        lost=_bool(body.get("lost_ticket")),
    )
    return _json(_gateout_payload(result))


@router.put("/lpr/gateoutcard")
async def lpr_gateoutcard(request: Request) -> JSONResponse:
    """Automated exit driven by an RFID card + plate.

    Port of ``GateoutController::GateOutRfidLpr``: the card is resolved as a
    member's entry or as a ticket code, the session is settled, and only the
    sparse status string is echoed back.
    """
    body = await _body(request)
    try:
        status = _service(request).gate_out_rfid(
            card=body.get("card"),
            gate=str(body.get("gate_out") or body.get("gate") or "2"),
            plate_num=body.get("plate_num"),
            url_gambar=body.get("url_gambar"),
            admin_id=_int(body.get("admin_id")),
            shift_id=_int(body.get("shift_id")),
        )
    except Exception:
        log.exception("PUT /api/lpr/gateoutcard failed")
        return _json({"status": "error"}, status_code=500)
    return _json({"status": status})


@router.post("/lpr/checkimagegateout")
async def check_image_gateout(request: Request) -> JSONResponse:
    """Look up an open session by plate, without settling it.

    Response matches ``checkLprImageGateOut``, including the nested
    ``image``/``gatein``/``gateout`` groups. Returns 404 when nothing matches —
    the same status the live site returns, though there it fires for a
    different reason (§7.7: the plate strings never agree).
    """
    body = await _body(request)
    plate_num = body.get("plate_num")
    url_image = body.get("url_image") or body.get("url_gambar")
    if not plate_num:
        return _json(
            {"status": "error", "message": "Missing plate_num"}, status_code=400
        )

    srv = _service(request)
    quote = srv.quote_gateout_image(plate=plate_num)
    if quote is None:
        return _json(
            {
                "status": "error",
                "message": "Active transaction not found for this plate_num",
                "plate_num": plate_num,
            },
            status_code=404,
        )

    available = False
    message = "No url_image provided"
    if url_image:
        try:
            probe = httpx.get(url_image, timeout=5, follow_redirects=True)
            if not probe.is_success:
                message = "Image is not available or unreachable"
            elif "image" not in probe.headers.get("content-type", ""):
                message = "URL is reachable but not an image"
            else:
                if srv.storage is not None:
                    srv.storage.download_async(
                        url_image,
                        "lpr/gateout",
                        srv.storage.lpr_filename(url_image, prefix="CAMOUT_LPR"),
                    )
                available = True
                message = "Image is available, download queued"
        except Exception as exc:
            message = f"Error checking image: {exc}"

    return _json(
        {
            "status": "success",
            "plate_num": plate_num,
            "image": {"available": available, "message": message, "url_image": url_image},
            "gatein": {
                "transaction_id": quote.transaction_id,
                "transaction_code": quote.transaction_code,
                "police_number": quote.police_number,
                "card_number": quote.card_number,
                "vehicle_id": quote.vehicle_id,
                "vehicle_name": quote.vehicle_name,
                "time_checkin": quote.time_checkin,
                "gate_in": quote.gate_in,
                "gate_status": quote.gate_status,
                "payment_status": quote.payment_status,
                "cam_in": quote.cam_in,
                "camin_lpr": quote.camin_lpr,
            },
            "gateout": {
                "gate_out": quote.gate_out,
                "cam_out": quote.cam_out,
                "camout_lpr": quote.camout_lpr,
            },
        }
    )


# ---------------------------------------------------------------------------
# Exit — the cashier path (the one that works in production)
# ---------------------------------------------------------------------------


@router.post("/gateout/detailtransaction")
async def detail_transaction(request: Request) -> JSONResponse:
    """Price a ticket for the cashier. Read-only.

    Response mirrors the captured one, including the Indonesian status strings
    and ``transaction: 'member'`` discriminator the frontend switches on.
    """
    body = await _body(request)
    result = _service(request).quote(
        code=body.get("transaction_code"),
        plate=body.get("police_number") or body.get("plate_num"),
    )
    if result.status == service.STATUS_NOT_FOUND:
        return _json(
            {"status": "notfound", "status_code": 404, "message": "transaction_notfound"},
            status_code=404,
        )

    payload: dict[str, Any] = {
        "status": "success",
        "status_code": 200,
        "transaction": "member" if result.is_member else "not_member",
        "data": {
            "member_code": result.plate_in if result.is_member else None,
            "name": result.member_name,
            "transaction_code": result.transaction_code,
            "vehicle_id": None,
            "time_checkin": result.time_checkin,
            "time_checkout": result.time_checkout,
            "duration": result.duration,
            "total": result.total,
            "cam_in": result.cam_in or "-",
            "cam_out": result.cam_out or "-",
            "payment_status": "lunas" if result.total == 0 else "belum_lunas",
            "police_number": result.plate_in,
            "breakdown": result.breakdown,
        },
    }
    # The captured member body carries message: success_member; the not-member
    # branch in Laravel omits it.
    if result.is_member:
        payload["message"] = "success_member"
    return _json(payload)


@router.put("/gateout/gateoutKasir")
async def gateout_kasir(request: Request) -> JSONResponse:
    """The cashier settles and releases the vehicle.

    In production this is the only path that works, and it never opens the
    exit barrier — nothing does (§7.6). Here it does.
    """
    body = await _body(request)
    result = _service(request).gate_out(
        gate=str(body.get("gate_out") or "2"),
        code=body.get("transaction_code"),
        plate_num=body.get("police_number") or body.get("plate_num"),
        admin_id=_int(body.get("admin_id")),
        shift_id=_int(body.get("shift_id")),
        lost=_bool(body.get("lost_ticket")),
    )
    if result.status == service.STATUS_TICKET_USED:
        return _json({"status": "already_paid", "message": result.message})
    if result.status == service.STATUS_NOT_FOUND:
        return _json(
            {"status": "notfound", "status_code": 404, "message": result.message},
            status_code=404,
        )
    return _json(
        {
            "status": "success",
            "status_code": 200,
            "data": _kasir_payload(result),
        }
    )


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    return _json({"status": "ok", "env": request.app.state.config.env})


@web_router.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse("/cashier")


@web_router.get("/cashier", include_in_schema=False)
async def cashier_page(request: Request) -> HTMLResponse:
    """The cashier desk, as a small web page.

    Served from the API itself so the desk only needs a browser pointed at
    ``http://<server>:8000/cashier``. Calls the same two endpoints the Tauri
    app uses (``detailtransaction`` then ``gateoutKasir``), then POSTs to the
    cashier's local gate-open daemon (``open_gate_url``) to raise the exit
    barrier — matching the captured Tauri behaviour.
    """
    config = getattr(request.app.state, "config", None)
    url = json.dumps(
        getattr(config, "open_gate_url", "")
        or "http://192.168.1.2:8090/open-gate"
    )
    return HTMLResponse(CASHIER_PAGE_HTML.replace("__OPEN_GATE_URL__", url))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _gateout_payload(result: service.GateOutResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "transaction_code": result.transaction_code,
        "total": result.total,
        "duration": result.duration,
        "police_number": result.plate_in,
        "plate_out": result.plate_out,
        "plate_match": result.plate_match,
        "time_checkin": result.time_checkin,
        "time_checkout": result.time_checkout,
        "cam_in": result.cam_in,
        "cam_out": result.cam_out,
        "member": result.is_member,
        "name": result.member_name,
        "breakdown": result.breakdown,
        "message": result.message,
    }


def _kasir_payload(result: service.GateOutResult) -> dict[str, Any]:
    """``responData()`` — the body ``gateoutKasir`` returns on success."""
    return {
        "transaction_code": result.transaction_code,
        "vehicle_id": result.vehicle_id,
        "time_checkin": result.time_checkin,
        "time_checkout": result.time_checkout,
        "duration": result.duration,
        "total": result.total,
        "cam_in": result.cam_in,
        "cam_out": result.cam_out,
        "payment_status": result.payment_status,
        "police_number": result.plate_in,
        "admin_id": result.admin_id,
        "shift_id": result.shift_id,
        "created_at": result.created_at,
        "updated_at": result.updated_at,
        "discount": "false",
    }


async def _body(request: Request) -> dict[str, Any]:
    """Accept JSON, form-urlencoded, or multipart.

    The cashier app sends multipart for ``detailtransaction`` and
    form-urlencoded for ``gateoutKasir`` (flow.md §6), while the LPR units send
    JSON, so all three have to work.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    if "form-data" in content_type or "x-www-form-urlencoded" in content_type:
        form = await request.form()
        return {key: form[key] for key in form}

    # Fall back to the query string, which is how checkimagegateout is called.
    return dict(request.query_params)


async def _body_and_file(request: Request) -> tuple[dict[str, Any], bytes | None]:
    """The parsed body and the ``image`` upload (multipart), else None.

    The LPR entry endpoints (``lpr/gatein``, ``lpr/gateinimage``) receive the
    photo as an ``image`` file in a multipart request. JSON bodies carry no
    file.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await _body(request), None

    form = await request.form()
    payload = {key: form[key] for key in form if key != "image"}
    image = form.get("image")
    if image is None or not hasattr(image, "read"):
        return payload, None
    return payload, await image.read()


def _int(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def create_app(config: Config, parking_service: ParkingService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("API up (env=%s)", config.env)
        yield
        if parking_service.storage is not None:
            parking_service.storage.shutdown()

    app = FastAPI(title="Trafix Parking API", version="1.0", lifespan=lifespan)
    app.state.config = config
    app.state.service = parking_service

    # The Tauri cashier app sends Origin: tauri://localhost, so preflight
    # handling is load-bearing (flow.md §9).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    app.include_router(web_router)

    storage_dir = Path(config.policies.storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/storage", StaticFiles(directory=storage_dir), name="storage")

    return app


def build(config: Config) -> FastAPI:
    """Wire the whole application from configuration."""
    from trafix.publisher import MqttPublisher
    from trafix.storage import SnapshotStore

    db.init_engine(config.database_url)
    db.create_all()
    with db.session_scope() as session:
        db.seed(session)

    store = SnapshotStore(config.policies.storage_dir)
    publisher = MqttPublisher(config)
    publisher.start()

    parking_service = ParkingService(
        db.new_session, publisher=publisher, storage=store, config=config
    )
    return create_app(config, parking_service)
