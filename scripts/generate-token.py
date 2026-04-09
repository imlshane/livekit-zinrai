#!/usr/bin/env python3
"""
Generate a LiveKit viewer JWT token for player.html.

Usage:
  python3 generate-token.py
  python3 generate-token.py --room my-room --identity viewer-001 --expire-days 7

Requirements: pip install pyjwt  (or use the stdlib hmac version below)
"""
import argparse
import hmac
import hashlib
import base64
import json
import time
import os

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def generate_token(api_key: str, api_secret: str, room: str, identity: str, expire_days: int) -> str:
    header  = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": api_key,
        "sub": identity,
        "iat": int(time.time()),
        "exp": int(time.time()) + expire_days * 86400,
        "video": {
            "room": room,
            "roomJoin": True,
            "canSubscribe": True,
            "canPublish": False,
            "canPublishData": False,
        },
    }
    h = b64url(json.dumps(header, separators=(',', ':')).encode())
    p = b64url(json.dumps(payload, separators=(',', ':')).encode())
    sig_input = f"{h}.{p}".encode()
    sig = hmac.new(api_secret.encode(), sig_input, hashlib.sha256).digest()
    return f"{h}.{p}.{b64url(sig)}"

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--room",        default="test-room")
    parser.add_argument("--identity",    default="viewer-001")
    parser.add_argument("--expire-days", type=int, default=7)
    args = parser.parse_args()

    api_key    = os.environ.get("LIVEKIT_API_KEY",    "YOUR_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "YOUR_API_SECRET")

    if "YOUR_" in api_key or "YOUR_" in api_secret:
        print("Set LIVEKIT_API_KEY and LIVEKIT_API_SECRET env vars first.")
        print("  export LIVEKIT_API_KEY=your-key")
        print("  export LIVEKIT_API_SECRET=your-secret")
        exit(1)

    token = generate_token(api_key, api_secret, args.room, args.identity, args.expire_days)
    print(f"\nToken (expires in {args.expire_days} days):\n")
    print(token)
    print("\nPaste this into nginx/html/player.html as the TOKEN value.")
