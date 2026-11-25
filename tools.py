import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from langchain_core.tools import tool
import requests

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
FLIGHTS_FILE = DATA_DIR / "flights.json"
BOOKINGS_FILE = DATA_DIR / "bookings.json"

DUFFEL_BASE_URL = "https://api.duffel.com"
DUFFEL_VERSION = "v2"
OFFER_CACHE: dict[str, dict] = {}
_duffel_search_failed: bool = False
RETRYABLE_STATUS_CODES = {502, 503, 504}


def _duffel_headers() -> dict:
    token = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DUFFEL_ACCESS_TOKEN is not set.")
    return {
        "Authorization": f"Bearer {token}",
        "Duffel-Version": DUFFEL_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _load_json(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _search_local_flights(
    origin: str,
    destination: str,
    date: str,
    max_fare_inr: int | None = None,
    reason: str | None = None,
) -> str:
    flights = _load_json(FLIGHTS_FILE)
    matches = [
        f
        for f in flights
        if f["origin"].upper() == origin
        and f["destination"].upper() == destination
        and f["date"] == date
        and f["seats_available"] > 0
        and (max_fare_inr is None or f["fare_inr"] <= max_fare_inr)
    ]
    if not matches:
        return json.dumps({
            "source": "local_fallback",
            "fallback_reason": reason,
            "status": "no_matches",
            "message": f"No available flights found for {origin} -> {destination} on {date}.",
        })
    matches.sort(key=lambda x: x["fare_inr"])
    return json.dumps({
        "source": "local_fallback",
        "fallback_reason": reason,
        "status": "ok",
        "flights": matches,
    })


def _book_local_flight(flight_id: str, passenger_name: str, passenger_age: int) -> str:
    flights = _load_json(FLIGHTS_FILE)
    bookings = _load_json(BOOKINGS_FILE)

    target = next((f for f in flights if f["flight_id"].upper() == flight_id.upper()), None)
    if not target:
        return json.dumps({"status": "error", "message": f"Flight {flight_id} not found."})
    if target["seats_available"] <= 0:
        return json.dumps({"status": "error", "message": f"No seats left on {flight_id}."})

    target["seats_available"] -= 1
    _save_json(FLIGHTS_FILE, flights)

    record = {
        "source": "local_fallback",
        "pnr": f"PNR-{uuid.uuid4().hex[:6].upper()}",
        "flight_id": target["flight_id"],
        "airline": target["airline"],
        "route": f"{target['origin']} -> {target['destination']}",
        "date": target["date"],
        "departure": target["departure"],
        "fare_inr": target["fare_inr"],
        "passenger_name": passenger_name.strip(),
        "passenger_age": passenger_age,
        "booked_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    bookings.append(record)
    _save_json(BOOKINGS_FILE, bookings)
    return json.dumps({"status": "confirmed", "booking": record})


def get_flight_by_id(flight_id: str) -> dict | None:
    """Look up a Duffel offer or local flight record by ID."""
    target = flight_id.strip()
    if target in OFFER_CACHE:
        return OFFER_CACHE[target]

    flights = _load_json(FLIGHTS_FILE)
    return next((f for f in flights if f["flight_id"].upper() == target.upper()), None)


@tool
def search_flights(
    origin: str,
    destination: str,
    date: str,
    max_fare_inr: int | None = None,
) -> str:
    """Search flights via Duffel API using 3-letter IATA codes and YYYY-MM-DD date. Falls back to data/flights.json if Duffel fails."""
    global _duffel_search_failed
    origin_code = origin.strip().upper()
    dest_code = destination.strip().upper()
    departure_date = date.strip()

    try:
        payload = {
            "data": {
                "slices": [{"origin": origin_code, "destination": dest_code, "departure_date": departure_date}],
                "passengers": [{"type": "adult"}],
                "cabin_class": "economy",
                "return_offers": True,
            }
        }
        resp = requests.post(
            f"{DUFFEL_BASE_URL}/air/offer_requests",
            headers=_duffel_headers(),
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()["data"]

        results = []
        for raw in data.get("offers") or []:
            amount_str = str(raw["total_amount"])
            currency = str(raw["total_currency"])
            if max_fare_inr is not None and currency.upper() == "INR" and float(amount_str) > max_fare_inr:
                continue

            first_slice = raw["slices"][0]
            first_seg = first_slice["segments"][0]
            last_seg = first_slice["segments"][-1]
            pax_list = raw.get("passengers") or data.get("passengers") or []
            offer_id = raw["id"]

            item = {
                "source": "duffel_api",
                "flight_id": offer_id,
                "passenger_id": pax_list[0]["id"] if pax_list else "pas_0",
                "airline": raw["owner"]["name"],
                "origin": origin_code,
                "destination": dest_code,
                "date": departure_date,
                "departure": first_seg["departing_at"],
                "arrival": last_seg["arriving_at"],
                "amount": amount_str,
                "currency": currency,
            }
            OFFER_CACHE[offer_id] = item
            results.append(item)

        _duffel_search_failed = False
        results.sort(key=lambda x: float(x["amount"]))
        if not results:
            return json.dumps({"source": "duffel_api", "status": "no_matches", "flights": []})
        return json.dumps({"source": "duffel_api", "status": "ok", "flights": results[:5]})

    except Exception as exc:
        _duffel_search_failed = True
        return _search_local_flights(origin_code, dest_code, departure_date, max_fare_inr, reason=str(exc))


@tool
def book_flight(flight_id: str, passenger_name: str, passenger_age: int) -> str:
    """Book a Duffel offer via /air/orders, or fall back to bookings.json only if Duffel flight search or order API fails."""
    clean_id = flight_id.strip()
    cached = get_flight_by_id(clean_id)

    if not clean_id.startswith("off_"):
        if not _duffel_search_failed:
            return json.dumps({
                "status": "error",
                "message": "Local fallback booking is only available when Duffel flight search fails.",
            })
        return _book_local_flight(clean_id, passenger_name, passenger_age)

    if not cached:
        return json.dumps({"status": "error", "message": f"Offer {clean_id} not found. Search flights first."})

    parts = passenger_name.strip().split()
    given_name = parts[0]
    family_name = " ".join(parts[1:]) if len(parts) > 1 else "Traveler"

    try:
        payload = {
            "data": {
                "type": "instant",
                "selected_offers": [clean_id],
                "passengers": [{
                    "id": cached.get("passenger_id", "pas_0"),
                    "title": "ms",
                    "gender": "f",
                    "given_name": given_name,
                    "family_name": family_name,
                    "born_on": "1998-05-10",
                    "email": "passenger@example.com",
                    "phone_number": "+919876543210",
                }],
                "payments": [{
                    "type": "balance",
                    "amount": str(cached["amount"]),
                    "currency": str(cached["currency"]),
                }],
            }
        }

        for attempt in range(3):
            resp = requests.post(
                f"{DUFFEL_BASE_URL}/air/orders",
                headers=_duffel_headers(),
                json=payload,
                timeout=25,
            )
            if resp.status_code not in RETRYABLE_STATUS_CODES:
                break
            time.sleep(1.5 * (attempt + 1))

        if not resp.ok:
            raise RuntimeError(f"Duffel HTTP {resp.status_code}: {resp.text}")

        order = resp.json()["data"]
        record = {
            "source": "duffel_api",
            "pnr": order.get("booking_reference") or f"DUF-{uuid.uuid4().hex[:6].upper()}",
            "order_id": order["id"],
            "flight_id": clean_id,
            "airline": cached["airline"],
            "route": f"{cached['origin']} -> {cached['destination']}",
            "date": cached["date"],
            "departure": cached["departure"],
            "amount": order.get("total_amount", cached["amount"]),
            "currency": order.get("total_currency", cached["currency"]),
            "passenger_name": passenger_name.strip(),
            "passenger_age": passenger_age,
            "booked_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        return json.dumps({"status": "confirmed", "booking": record})

    except Exception as exc:
        bookings = _load_json(BOOKINGS_FILE)
        record = {
            "source": "local_fallback",
            "fallback_reason": str(exc),
            "pnr": f"FBK-{uuid.uuid4().hex[:6].upper()}",
            "flight_id": clean_id,
            "airline": cached["airline"],
            "route": f"{cached['origin']} -> {cached['destination']}",
            "date": cached["date"],
            "departure": cached["departure"],
            "amount": cached["amount"],
            "currency": cached["currency"],
            "passenger_name": passenger_name.strip(),
            "passenger_age": passenger_age,
            "booked_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        bookings.append(record)
        _save_json(BOOKINGS_FILE, bookings)
        return json.dumps({"status": "confirmed_via_fallback", "booking": record})