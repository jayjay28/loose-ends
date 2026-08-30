# Loose Ends push relay

The one server Loose Ends runs. Self-hosted engines can't sign APNs pushes
(that takes the app's private key), so this signs and forwards — and it is
content-free by construction: the push API has no title or body field. Every
push says a fixed placeholder, marked mutable-content, and the phone fetches
the real words from its own engine.

Stateless: install secrets are HMACs of install ids under RELAY_SIGNING_KEY,
so there is no database. Revocation = RELAY_DENYLIST (comma-separated ids).

## Env
- RELAY_SIGNING_KEY   long random string; rotating it invalidates every install
- APNS_KEY_PATH       the .p8 (mount as a secret)
- APNS_KEY_ID, APNS_TEAM_ID
- APNS_TOPIC          default com.lifelinecly.app
- APNS_SANDBOX        1 for development builds
- RELAY_DENYLIST      optional

## Deploy (Cloud Run)
    gcloud run deploy loose-ends-relay --source . --region us-east1 \
      --allow-unauthenticated --max-instances 2 \
      --set-secrets "/secrets/apns.p8=apns-key:latest" \
      --set-env-vars "APNS_KEY_PATH=/secrets/apns.p8,APNS_KEY_ID=...,APNS_TEAM_ID=...,RELAY_SIGNING_KEY=..."

## Test
    ../backend/.venv/bin/python -m pytest test_relay.py

## Live
https://loose-ends-relay-709239701189.us-east1.run.app (Cloud Run,
us-east1, APNS_SANDBOX=1 until App Store launch — flip to 0
then). Health: GET /v1/health.
