#!/usr/bin/env python3
"""
Zinrai LiveKit Setup
--------------------
Interactive script that generates all config files from your answers.
Run once on a fresh server (or after git clone) to get started.

Usage:
    python3 setup.py              # full interactive setup
    python3 setup.py --regen      # regenerate configs without re-asking (uses .env)
"""

import argparse
import hmac
import hashlib
import base64
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

# ── helpers ────────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default

def ask_secret(prompt: str) -> str:
    import getpass
    val = getpass.getpass(f"{prompt}: ").strip()
    return val

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def gen_secret() -> str:
    return secrets.token_hex(32)

def make_jwt(api_key: str, api_secret: str, grants: dict, ttl_seconds: int = 600) -> str:
    header  = {"alg": "HS256", "typ": "JWT"}
    payload = {"iss": api_key, "sub": "admin",
                "iat": int(time.time()), "exp": int(time.time()) + ttl_seconds,
                "video": grants}
    h = b64url(json.dumps(header, separators=(",", ":")).encode())
    p = b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(api_secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{b64url(sig)}"

def make_viewer_token(api_key: str, api_secret: str, room: str,
                      identity: str = "viewer-001", expire_days: int = 30) -> str:
    header  = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": api_key, "sub": identity,
        "iat": int(time.time()), "exp": int(time.time()) + expire_days * 86400,
        "video": {"room": room, "roomJoin": True,
                  "canSubscribe": True, "canPublish": False, "canPublishData": False},
    }
    h = b64url(json.dumps(header, separators=(",", ":")).encode())
    p = b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(api_secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{b64url(sig)}"

def write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  wrote  {path.relative_to(ROOT)}")

def section(title: str):
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print('─' * 50)

# ── config collection ──────────────────────────────────────────────────────────

def collect_config() -> dict:
    print("\n┌─────────────────────────────────────────────┐")
    print("│   Zinrai LiveKit — Interactive Setup        │")
    print("└─────────────────────────────────────────────┘\n")

    section("Server")
    server_ip = ask("Server public IP")
    while not server_ip:
        print("  ✗ Server IP is required.")
        server_ip = ask("Server public IP")

    section("Domain")
    domain = ask("Domain name (e.g. livestream.zinrai.live)")
    while not domain:
        print("  ✗ Domain is required.")
        domain = ask("Domain name")

    section("LiveKit API credentials")
    print("  Leave blank to auto-generate a secure key/secret.\n")
    api_key = ask("API key name", default="lk-key")
    regen   = ask("Auto-generate API secret? (y/n)", default="y").lower() == "y"
    if regen:
        api_secret = gen_secret()
        print(f"  ✓ Generated secret: {api_secret}")
    else:
        api_secret = ask_secret("API secret (input hidden)")
        while not api_secret:
            print("  ✗ Secret cannot be empty.")
            api_secret = ask_secret("API secret")

    section("Redis")
    print("  1) DigitalOcean Managed Redis (recommended — TLS, persistent)")
    print("  2) Local Redis container (simpler, no TLS)\n")
    redis_choice = ask("Choose Redis backend", default="1")

    if redis_choice == "1":
        redis_host     = ask("DO Redis hostname (from DO dashboard)")
        redis_port     = ask("DO Redis port", default="25061")
        redis_password = ask_secret("DO Redis password (input hidden)")
        redis_config = {
            "type": "do",
            "host": redis_host,
            "port": redis_port,
            "password": redis_password,
        }
    else:
        redis_config = {"type": "local"}

    section("Stream room")
    room = ask("LiveKit room name", default="test-room")

    return {
        "server_ip":    server_ip,
        "domain":       domain,
        "api_key":      api_key,
        "api_secret":   api_secret,
        "redis":        redis_config,
        "room":         room,
    }

def load_env() -> dict:
    env_file = ROOT / ".env"
    if not env_file.exists():
        print("✗ .env not found — run setup.py without --regen first.")
        sys.exit(1)
    cfg = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()

    redis_type = cfg.get("REDIS_TYPE", "local")
    redis_config = {"type": redis_type}
    if redis_type == "do":
        redis_config.update({
            "host":     cfg["REDIS_HOST"],
            "port":     cfg.get("REDIS_PORT", "25061"),
            "password": cfg["REDIS_PASSWORD"],
        })
    return {
        "server_ip":  cfg["SERVER_IP"],
        "domain":     cfg["DOMAIN"],
        "api_key":    cfg["LIVEKIT_API_KEY"],
        "api_secret": cfg["LIVEKIT_API_SECRET"],
        "redis":      redis_config,
        "room":       cfg.get("ROOM", "test-room"),
    }

# ── file generation ────────────────────────────────────────────────────────────

def redis_yaml_block(redis: dict, indent: int = 0) -> str:
    pad = " " * indent
    if redis["type"] == "do":
        return (
            f"{pad}redis:\n"
            f"{pad}  address: {redis['host']}:{redis['port']}\n"
            f"{pad}  password: {redis['password']}\n"
            f"{pad}  use_tls: true\n"
        )
    else:
        return (
            f"{pad}redis:\n"
            f"{pad}  address: 127.0.0.1:6379\n"
        )

def gen_livekit_yaml(cfg: dict) -> str:
    return f"""port: 7880
bind_addresses:
  - 127.0.0.1

rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 51000
  use_external_ip: true
  node_ip: {cfg['server_ip']}

keys:
  {cfg['api_key']}: {cfg['api_secret']}

{redis_yaml_block(cfg['redis'])}
logging:
  level: info

room:
  empty_timeout: 300
  departure_timeout: 20
"""

def gen_ingress_yaml(cfg: dict) -> str:
    return f"""api_key: {cfg['api_key']}
api_secret: {cfg['api_secret']}
ws_url: ws://localhost:7880
rtmp:
  port: 1935
logging:
  level: info

{redis_yaml_block(cfg['redis'])}"""

def gen_nginx_conf(cfg: dict) -> str:
    d = cfg["domain"]
    return f"""worker_processes auto;
events {{ worker_connections 4096; }}

http {{
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    server {{
        listen 80;
        server_name {d};
        location /.well-known/acme-challenge/ {{ root /var/www/certbot; }}
        location / {{ return 301 https://$host$request_uri; }}
    }}

    server {{
        listen 443 ssl;
        http2 on;
        server_name {d};

        ssl_certificate     /etc/letsencrypt/live/{d}/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/{d}/privkey.pem;
        ssl_protocols       TLSv1.2 TLSv1.3;

        root /usr/share/nginx/html;

        location /player {{
            alias /usr/share/nginx/html/player.html;
            default_type text/html;
            add_header Cache-Control no-cache;
        }}

        location / {{
            proxy_pass         http://127.0.0.1:7880;
            proxy_http_version 1.1;
            proxy_set_header   Upgrade    $http_upgrade;
            proxy_set_header   Connection upgrade;
            proxy_set_header   Host       $host;
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
        }}
    }}
}}
"""

def gen_env(cfg: dict) -> str:
    r = cfg["redis"]
    lines = [
        f"SERVER_IP={cfg['server_ip']}",
        f"DOMAIN={cfg['domain']}",
        f"LIVEKIT_API_KEY={cfg['api_key']}",
        f"LIVEKIT_API_SECRET={cfg['api_secret']}",
        f"ROOM={cfg['room']}",
        f"REDIS_TYPE={r['type']}",
    ]
    if r["type"] == "do":
        lines += [
            f"REDIS_HOST={r['host']}",
            f"REDIS_PORT={r.get('port', '25061')}",
            f"REDIS_PASSWORD={r['password']}",
        ]
    return "\n".join(lines) + "\n"

def gen_docker_compose(cfg: dict) -> str:
    redis_service = ""
    livekit_extra = ""
    ingress_extra = ""

    if cfg["redis"]["type"] == "local":
        redis_service = """
  redis:
    image: redis:7-alpine
    container_name: livekit-redis
    restart: unless-stopped
    network_mode: host
    command: redis-server --bind 127.0.0.1 --port 6379
"""
        livekit_extra = "\n    depends_on:\n      - redis"
        ingress_extra = "\n    depends_on:\n      - livekit\n      - redis"
    else:
        ingress_extra = "\n    depends_on:\n      - livekit"

    return f"""services:
{redis_service}
  livekit:
    image: livekit/livekit-server:latest
    container_name: livekit
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./livekit.yaml:/etc/livekit.yaml
    command: --config /etc/livekit.yaml{livekit_extra}

  ingress:
    image: livekit/ingress:latest
    container_name: livekit-ingress
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./ingress.yaml:/etc/ingress.yaml
    environment:
      - INGRESS_CONFIG_FILE=/etc/ingress.yaml{ingress_extra}

  nginx:
    image: nginx:alpine
    container_name: livekit-nginx
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/html:/usr/share/nginx/html:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
"""

# ── ingress + token ────────────────────────────────────────────────────────────

def create_ingress_and_token(cfg: dict):
    import urllib.request
    import urllib.error

    section("Creating RTMP ingress")
    admin_jwt = make_jwt(cfg["api_key"], cfg["api_secret"],
                         {"roomCreate": True, "ingressAdmin": True})
    payload = json.dumps({
        "input_type": 0,
        "room_name": cfg["room"],
        "participant_identity": "stream-host",
        "participant_name": "Stream Host",
        "enable_transcoding": True,
        "video": {"preset": 3},  # H264_720P_30FPS_1_LAYER
    }).encode()

    req = urllib.request.Request(
        "http://127.0.0.1:7880/twirp/livekit.Ingress/CreateIngress",
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {admin_jwt}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        stream_key = result["stream_key"]
        ingress_id = result["ingress_id"]
        preset     = result.get("video", {}).get("preset", "?")
        print(f"  ✓ Ingress created")
        print(f"    ingress_id : {ingress_id}")
        print(f"    stream_key : {stream_key}")
        print(f"    preset     : {preset}")
        print(f"\n  OBS RTMP URL   : rtmp://{cfg['server_ip']}/live")
        print(f"  OBS Stream Key : {stream_key}")
        return stream_key
    except Exception as e:
        print(f"  ✗ Could not create ingress: {e}")
        print("    Make sure LiveKit is running: docker compose up -d")
        return None

def update_player_token(cfg: dict):
    section("Generating viewer token (30 days)")
    token = make_viewer_token(cfg["api_key"], cfg["api_secret"], cfg["room"])
    player_path = ROOT / "nginx" / "html" / "player.html"
    html = player_path.read_text()

    # Replace either a real token or the placeholder
    import re
    html = re.sub(r"const TOKEN\s*=\s*'[^']*'",
                  f"const TOKEN  = '{token}'", html)
    domain = cfg["domain"]
    html = re.sub(r"const WS_URL\s*=\s*'[^']*'",
                  f"const WS_URL = 'wss://{domain}'", html)
    player_path.write_text(html)
    print(f"  ✓ Token injected into nginx/html/player.html")
    print(f"    expires : {time.strftime('%Y-%m-%d', time.localtime(time.time() + 30*86400))}")
    return token

# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--regen", action="store_true",
                        help="Regenerate configs from existing .env (skip prompts)")
    parser.add_argument("--token-only", action="store_true",
                        help="Only regenerate the viewer token")
    args = parser.parse_args()

    if args.regen or args.token_only:
        cfg = load_env()
    else:
        cfg = collect_config()

    if args.token_only:
        update_player_token(cfg)
        print("\n  Restart nginx to apply:  docker compose restart nginx")
        return

    section("Writing config files")
    write_file(ROOT / ".env",                       gen_env(cfg))
    write_file(ROOT / "livekit.yaml",               gen_livekit_yaml(cfg))
    write_file(ROOT / "ingress.yaml",               gen_ingress_yaml(cfg))
    write_file(ROOT / "nginx" / "nginx.conf",       gen_nginx_conf(cfg))
    write_file(ROOT / "docker-compose.yml",         gen_docker_compose(cfg))

    update_player_token(cfg)

    # Only attempt ingress creation if LiveKit appears to be running
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:7880", timeout=2)
        create_ingress_and_token(cfg)
    except Exception:
        print("\n  (Skipping ingress creation — LiveKit not reachable yet)")
        print("  After 'docker compose up -d', run:")
        print("    python3 setup.py --regen")
        print("  Or create the ingress manually:")
        print("    bash scripts/create-ingress.sh")

    section("Done")
    print(f"""
  Next steps:
  1. Start services   :  docker compose up -d
  2. Check logs       :  docker compose logs -f
  3. Create ingress   :  python3 setup.py --regen   (if not created above)
  4. Watch stream     :  https://{cfg['domain']}/player

  OBS RTMP Server : rtmp://{cfg['server_ip']}/live
  Player URL      : https://{cfg['domain']}/player
""")

if __name__ == "__main__":
    main()
