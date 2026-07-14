"""Simulates several independent applications querying Hasura, each with its own
JWT credential, role, and data slice. Stdlib only — no pip installs needed.

    python consumers/simulate.py

Each app's JWT carries x-hasura-allowed-roles / x-hasura-default-role (and for the
kiosk, an x-hasura-borough claim that drives a row-level filter). In production the
tokens would come from an IdP (Auth0/Cognito/Keycloak) signing RS256, verified by
Hasura via a JWKS URL — the mechanics below are identical, only the signer changes.
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

HASURA_URL = "http://localhost:8080/v1/graphql"


def jwt_key():
    for line in (Path(__file__).parent.parent / ".env").read_text().splitlines():
        if line.startswith("HASURA_JWT_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("HASURA_JWT_KEY not found in .env")


def b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def mint_jwt(key: str, app: str, role: str, extra_claims: dict) -> str:
    """HS256 JWT with Hasura's claims namespace."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": app,
        "iat": now,
        "exp": now + 3600,
        "https://hasura.io/jwt/claims": {
            "x-hasura-allowed-roles": [role],
            "x-hasura-default-role": role,
            **extra_claims,
        },
    }
    signing_input = b64url(json.dumps(header).encode()) + b"." + b64url(json.dumps(payload).encode())
    sig = hmac.new(key.encode(), signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + b64url(sig)).decode()


def graphql(query: str, token: str | None):
    req = urllib.request.Request(
        HASURA_URL,
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        body = json.load(e)
    ms = (time.perf_counter() - t0) * 1000
    return body, ms


# app name, hasura role, extra JWT claims, [(description, query, expect_denied)]
APPS = [
    (
        "ops-monitor",
        "ops",
        {},
        [
            ("last pipeline runs",
             "{ analytics__export_runs(order_by:{started_at:desc}, limit:3){ run_id status finished_at } }",
             False),
            ("business data (should be DENIED)",
             "{ analytics_zone_daily_stats(limit:1){ trips } }",
             True),
        ],
    ),
    (
        "city-dashboard",
        "analyst",
        {},
        [
            ("january city-wide totals",
             "{ analytics_zone_daily_stats_aggregate { aggregate { sum { trips total_revenue } } } }",
             False),
            ("top zone-days by revenue",
             "{ analytics_zone_daily_stats(order_by:{total_revenue:desc}, limit:3){ day total_revenue taxi_zone { zone_name } } }",
             False),
            ("pipeline internals (should be DENIED)",
             "{ analytics__export_runs(limit:1){ run_id } }",
             True),
        ],
    ),
    (
        "kiosk-manhattan",
        "kiosk",
        {"x-hasura-borough": "Manhattan"},
        [
            ("zones — row filter comes from the JWT claim, not the query",
             "{ analytics_taxi_zones(limit:4, order_by:{zone_name:asc}){ zone_name borough } }",
             False),
            ("stats (should be DENIED)",
             "{ analytics_zone_daily_stats(limit:1){ trips } }",
             True),
        ],
    ),
]


def main():
    key = jwt_key()
    failures = 0
    for app, role, claims, queries in APPS:
        token = mint_jwt(key, app, role, claims)
        print(f"\n=== {app}  (role: {role}{', claims: ' + json.dumps(claims) if claims else ''}) ===")
        for desc, query, expect_denied in queries:
            body, ms = graphql(query, token)
            denied = "errors" in body
            ok = denied == expect_denied
            failures += 0 if ok else 1
            mark = "OK " if ok else "!! "
            if denied:
                print(f"  {mark}{desc}  [{ms:.0f} ms]")
                print(f"      denied: {body['errors'][0]['message']}")
            else:
                print(f"  {mark}{desc}  [{ms:.0f} ms]")
                print(f"      {json.dumps(body['data'])[:150]}")

    print("\n=== anonymous (no token — should be rejected) ===")
    body, ms = graphql("{ analytics_taxi_zones(limit:1){ zone_id } }", token=None)
    rejected = "errors" in body
    failures += 0 if rejected else 1
    print(f"  {'OK ' if rejected else '!! '}rejected: {body.get('errors', [{}])[0].get('message', body)}  [{ms:.0f} ms]")

    print(f"\n{'all access boundaries behaved as expected' if failures == 0 else f'{failures} UNEXPECTED result(s)'}")
    raise SystemExit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
