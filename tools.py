import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from langchain_core.tools import tool

DATA_DIR = Path(__file__).parent / "data"
FLIGHTS_FILE = DATA_DIR / "flights.json"
BOOKINGS_FILE = DATA_DIR / "bookings.json"


def _load_json(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def get_flight_by_id(flight_id: str) -> dict | None:
    """Look up a single flight record by flight_id for display or booking."""
    flights = _load_json(FLIGHTS_FILE)
    target_id = flight_id.strip().upper()
    return next((f for f in flights if f["flight_id"].upper() == target_id), None)


@tool
def search_flights(
    origin: str,
    destination: str,
    date: str,
    max_fare_inr: int | None = None,
) -> str:
    """Search available flights by 3-letter IATA airport codes (e.g. DEL, BOM, BLR), YYYY-MM-DD date, and optional maximum fare in INR."""
    origin_code = origin.strip().upper()
    dest_code = destination.strip().upper()
    flights = _load_json(FLIGHTS_FILE)

    matches = [
        f
        for f in flights
        if f["origin"] == origin_code
        and f["destination"] == dest_code
        and f["date"] == date.strip()
        and f["seats_available"] > 0
        and (max_fare_inr is None or f["fare_inr"] <= max_fare_inr)
    ]

    if not matches:
        return json.dumps({
            "status": "no_matches",
            "message": f"No available flights found for {origin_code} -> {dest_code} on {date} within budget {max_fare_inr}."
        })

    matches.sort(key=lambda x: x["fare_inr"])
    return json.dumps({"status": "ok", "flights": matches})


@tool
def book_flight(flight_id: str, passenger_name: str, passenger_age: int) -> str:
    """Create a confirmed flight booking in bookings.json and decrement available seats. Only call when the user has confirmed booking details."""
    flights = _load_json(FLIGHTS_FILE)
    bookings = _load_json(BOOKINGS_FILE)

    target = next((f for f in flights if f["flight_id"].upper() == flight_id.strip().upper()), None)
    if not target:
        return json.dumps({"status": "error", "message": f"Flight {flight_id} not found."})
    if target["seats_available"] <= 0:
        return json.dumps({"status": "error", "message": f"No seats left on {flight_id}."})

    target["seats_available"] -= 1
    _save_json(FLIGHTS_FILE, flights)

    pnr = f"PNR-{uuid.uuid4().hex[:6].upper()}"
    record = {
        "pnr": pnr,
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