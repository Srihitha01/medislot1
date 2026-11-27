"""Live 20-client booking contention test for a deployed MediSlot API.

Required environment: MEDISLOT_API_BASE_URL, TEST_REGISTRATION_DOMAIN,
TEST_REGISTRATION_PASSWORD. It creates 20 dedicated patient accounts and uses
one future capacity-3 slot. Successful appointments are cancelled at the end.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

BASE = os.environ["MEDISLOT_API_BASE_URL"].rstrip("/")
DOMAIN = os.environ["TEST_REGISTRATION_DOMAIN"].strip()
PASSWORD = os.environ["TEST_REGISTRATION_PASSWORD"]
if not DOMAIN or "@" in DOMAIN or "." not in DOMAIN:
    raise ValueError("TEST_REGISTRATION_DOMAIN must be a controlled email domain")
if len(PASSWORD) < 8:
    raise ValueError("TEST_REGISTRATION_PASSWORD must have at least 8 characters")


def request(method, path, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = "Bearer " + token
    return requests.request(method, BASE + path, headers=headers, timeout=35, **kwargs)


def create_patient(index):
    # Numeric timestamp suffix, not a UUID. DB user_id remains sequential AUTO_INCREMENT.
    email = f"medislot-contention-{time.time_ns()}-{index}@{DOMAIN}"
    response = request("POST", "/auth/register", json={
        "role": "patient", "name": f"MediSlot Contention {index}",
        "email": email, "password": PASSWORD, "phone": "9999999999", "city": "Hyderabad",
    })
    if response.status_code not in (200, 201):
        raise AssertionError(f"Registration failed ({response.status_code}): {response.text}")
    login = request("POST", "/auth/login", json={"email": email, "password": PASSWORD})
    if login.status_code != 200:
        raise AssertionError(f"Login failed ({login.status_code}): {login.text}")
    body = login.json()
    return body["accessToken"], email


def main():
    patients = [create_patient(i) for i in range(20)]
    token = patients[0][0]
    doctors = request("GET", "/doctors/search", token, params={"page": 1, "pageSize": 50})
    doctors.raise_for_status()
    now = datetime.now(timezone.utc)
    selected = None
    for doctor in doctors.json().get("items", []):
        slots = request("GET", f"/doctors/{doctor['doctorId']}/slots", token, params={
            "startFrom": now.isoformat(), "startTo": (now + timedelta(days=14)).isoformat(),
        })
        if slots.status_code != 200:
            continue
        for slot in slots.json().get("items", []):
            if int(slot.get("capacity", 0)) == 3 and int(slot.get("freeSeats", 0)) == 3:
                selected = slot
                break
        if selected:
            break
    if not selected:
        raise AssertionError("No future, empty capacity-3 slot found in the next 14 days")

    slot_id = selected["slotId"]
    def book(index):
        idem = f"milestone5-contention-{time.time_ns()}-{index}"
        response = request("POST", "/appointments", patients[index][0],
                           headers={"Idempotency-Key": idem}, json={"slotId": slot_id})
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}
        return index, idem, response.status_code, body

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(book, i) for i in range(20)]
        for future in as_completed(futures):
            results.append(future.result())

    successes = [r for r in results if r[2] in (200, 201)]
    conflicts = [r for r in results if r[2] == 409]
    unexpected = [r for r in results if r[2] not in (200, 201, 409)]
    if len(successes) != 3 or len(conflicts) != 17 or unexpected:
        raise AssertionError(
            f"Expected exactly 3 success + 17 conflict; got {len(successes)} success, "
            f"{len(conflicts)} conflict, unexpected={[(r[2], r[3]) for r in unexpected]}"
        )

    # Replay the exact successful key and prove the same appointment is returned.
    index, idem, _, first = successes[0]
    replay = request("POST", "/appointments", patients[index][0],
                     headers={"Idempotency-Key": idem}, json={"slotId": slot_id})
    replay.raise_for_status()
    replay_body = replay.json()
    if int(replay_body.get("appointmentId", -1)) != int(first.get("appointmentId", -2)):
        raise AssertionError("Idempotency replay did not return the original appointment")

    # Clean up test appointments through the normal cancellation endpoint.
    cleanup_errors = []
    for index, _, _, body in successes:
        aid = body.get("appointmentId")
        if aid:
            response = request("POST", f"/appointments/{aid}/cancel", patients[index][0], json={})
            if response.status_code != 200:
                cleanup_errors.append((aid, response.status_code, response.text))
    if cleanup_errors:
        raise AssertionError(f"Test appointments could not all be cancelled: {cleanup_errors}")

    print(f"PASS: slot={slot_id}; 20 simultaneous attempts; exactly 3 bookings; 17 conflicts; idempotency replay returned appointment #{first['appointmentId']}; test bookings cancelled")


if __name__ == "__main__":
    main()
