"""Smoke-boot the app and hit key endpoints. Not part of the test suite."""
from fastapi.testclient import TestClient

from hotfix_booking.app import create_app

app = create_app()
c = TestClient(app)

r1 = c.get("/health")
print("health:", r1.status_code, r1.json())

r2 = c.get("/")
has_marker = 'id="hotfix-booking"' in r2.text
print("index:", r2.status_code, f"({len(r2.text)} bytes, marker={has_marker})")

r3 = c.get("/api/hotfix-booking/bookings")
print("bookings:", r3.status_code, r3.json())

# Static assets
for path in ("/hotfix-booking.js", "/utils.js", "/styles.css"):
    r = c.get(path)
    print(f"{path}: {r.status_code} ({len(r.content)} bytes)")
