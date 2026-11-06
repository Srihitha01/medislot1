"""Live tests against deployed API; no mocks. Requires two real patient test accounts."""
import os
import uuid
import requests
from datetime import datetime, timedelta, timezone

BASE = os.environ["MEDISLOT_API_BASE_URL"].rstrip("/")
def call(method, path, token=None, **kwargs):
    headers = {"Authorization": "Bearer "+token} if token else {}
    return requests.request(method, BASE+path, headers=headers, timeout=25, **kwargs)
def login(email, password):
    r=call("POST","/auth/login",json={"email":email,"password":password})
    r.raise_for_status()
    data=r.json()
    assert data["role"]=="patient", "Test user must be a patient"
    return data["accessToken"],str(data["userId"])
def main():
    # Register a unique test patient through the deployed registration Lambda.
    email = "medislot-ci-" + uuid.uuid4().hex[:12] + "@" + os.environ["TEST_REGISTRATION_DOMAIN"]
    password = os.environ["TEST_REGISTRATION_PASSWORD"]
    created = call("POST", "/auth/register", json={
        "role": "patient", "name": "MediSlot Integration Test",
        "email": email, "password": password, "phone": "9999999999",
        "city": "Hyderabad"
    })
    assert created.status_code in (200, 201), created.text
    registered_token, _ = login(email, password)
    assert call("GET", "/patients/me", registered_token).status_code == 200
    a, aid=login(os.environ["TEST_PATIENT_A_EMAIL"],os.environ["TEST_PATIENT_A_PASSWORD"])
    b, bid=login(os.environ["TEST_PATIENT_B_EMAIL"],os.environ["TEST_PATIENT_B_PASSWORD"])
    assert aid!=bid
    mine=call("GET","/patients/me",a)
    assert mine.status_code==200, mine.text
    # Attempt direct object-reference access as patient A to patient B's ID.
    forbidden=call("GET","/patients/"+bid,a)
    assert forbidden.status_code in (403,404), forbidden.text
    search=call("GET","/doctors/search",a,params={"page":1,"pageSize":10})
    assert search.status_code==200,search.text
    data=search.json()
    assert isinstance(data.get("items"),list) and len(data["items"])<=10
    for field in ("name","hospital","specialisation","city"):
        if data["items"]:
            value=data["items"][0].get(field)
            if value:
                r=call("GET","/doctors/search",a,params={field:value,"pageSize":10})
                assert r.status_code==200,(field,r.text)
    if data["items"]:
        doctor_id=data["items"][0]["doctorId"]
        p=call("GET",f"/doctors/{doctor_id}",a)
        assert p.status_code==200,p.text
        now=datetime.now(timezone.utc)
        r=call("GET",f"/doctors/{doctor_id}/slots",a,params={
            "startFrom":now.isoformat(),"startTo":(now+timedelta(days=7)).isoformat()})
        assert r.status_code==200,r.text
        for slot in r.json()["items"]:
            assert slot["freeSeats"]==slot["capacity"]-slot["bookedCount"]>0
    print("PASS: live patient isolation, pagination, doctor search, profile and slots")
if __name__=="__main__":main()
