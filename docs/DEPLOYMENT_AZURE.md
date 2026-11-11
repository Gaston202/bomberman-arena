# Deploying the TCP version on Azure (and making it work on the university network)

This document explains how the **TCP version** of Bomberman Arena is hosted on an Azure virtual machine,
why the first setup did **not** work on the university Wi-Fi, how we diagnosed it, and what we changed
so that it does.

**Play here:** https://bomberman-arena.germanywestcentral.cloudapp.azure.com

---

## 1. Final architecture

```
                      Internet / university Wi-Fi
                                 │
                 HTTPS + wss://  │  port 443 (encrypted, looks like any website)
                                 ▼
┌──────────────────────── Azure VM (Ubuntu 24.04, 20.79.184.8) ────────────────────────┐
│                                                                                      │
│   nginx  (port 80 → redirect to HTTPS, port 443 → TLS with a Let's Encrypt cert)     │
│     │                                                                                │
│     │  plain HTTP / WebSocket, only inside the VM                                    │
│     ▼                                                                                │
│   python3 tcp_version/server_tcp.py   (port 8000, managed by systemd)                │
│     └── data/arena.db  (SQLite: accounts, scores, match history)                     │
│                                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

| Component | Role |
|---|---|
| **Azure VM** (Standard B1s/B1ms, Ubuntu Server 24.04 LTS) | A small Linux server with a public IP |
| **Network Security Group (NSG)** | Azure's firewall: decides which ports are reachable from the internet |
| **Azure DNS name label** | Free domain `bomberman-arena.germanywestcentral.cloudapp.azure.com` → `20.79.184.8` |
| **systemd** service `bomberman-tcp` | Starts the game server at boot, restarts it if it crashes |
| **nginx** | Reverse proxy: receives HTTPS on 443 and forwards to the game server on 8000 |
| **certbot / Let's Encrypt** | Free TLS certificate, renewed automatically |

The game code did **not** need to change: `tcp_version/web/index.html` already picks the WebSocket scheme
from the page's protocol:

```js
ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
```

So when the page is loaded over `https://`, the game traffic automatically goes over encrypted `wss://`.

---

## 2. The problem: the game froze on the university Wi-Fi

### First setup (did not work on campus)
The first version exposed the Python server directly to the internet:

```
Browser ──ws:// plain TCP, port 8000──► server_tcp.py
```

- The page loaded and the lobby worked (login, create/join a room).
- **About 4–5 seconds into every match, the connection froze.** After 6 seconds without data the server
  dropped the players: `disconnected (no packet for 6s)`.
- The **same test from a mobile hotspot worked perfectly** (91/91 snapshots received, 0 lost),
  and it also worked on `localhost`.

So the code and the server were fine, and the problem was on the **network path** of the university.

### How we diagnosed it
1. **Reproduce it with bots.** `tools/bots.py` plays a full match automatically. It failed 100% of the time
   over the university network and succeeded locally, so it wasn't a random glitch.
2. **Read the server log** (`journalctl -u bomberman-tcp`). The match started and ended normally on the
   server, but the clients never received the `end` message and stopped sending packets.
3. **Capture packets on the VM** with `tcpdump` during a match:
   - At one instant, **all traffic from the client stopped at the same time**, even tiny 36-byte input packets.
   - The server kept sending, then retransmitted the same data with growing gaps (1 s, 2 s, 4 s, 8 s):
     classic TCP retransmission backoff, because nothing was acknowledged anymore.
4. **Rule out a wrong theory: MTU.** We noticed the client's MSS had been rewritten to 1250 on the way,
   and the first retransmitted packets were large (1232 bytes), which looks like an *MTU black hole*
   (large packets dropped silently). We tested it by forcing the server to send only 1000-byte segments
   (`iptables ... TCPMSS --set-mss 1000`). **It still froze**, so packet size was **not** the cause.
   (That temporary rule was removed afterwards.)
5. **Conclusion.** Something on the university network (a firewall / traffic filter) cuts a connection
   that carries a steady stream of unencrypted real-time traffic to an unusual port (8000) after a few seconds.
   The mobile network does not do this.

> Lesson: when a network app works on one network and fails on another, capture packets on the server
> (`tcpdump`) before changing code. And test each theory: we had a convincing MTU theory and the test
> proved it wrong.

---

## 3. The fix: serve the game as a normal HTTPS website on port 443

University firewalls almost always allow **port 443 (HTTPS)**, because blocking it would break the web.
And with **TLS encryption**, the firewall cannot look inside the traffic to see that it's a game stream.
It only sees an ordinary encrypted connection to a normal-looking domain.

To serve HTTPS we need:
1. a **domain name** (Let's Encrypt doesn't issue certificates for bare IP addresses),
2. a **TLS certificate** for that domain,
3. something that speaks HTTPS and forwards to our Python server: **nginx** as a *reverse proxy*.

We chose nginx (rather than Caddy) because it is the most widely used reverse proxy and is available
directly from Ubuntu's own package repository.

---

## 4. Step-by-step setup

### 4.1 Create the VM (Azure portal)
| Setting | Value |
|---|---|
| Image | **Ubuntu Server 24.04 LTS – x64 Gen2** (Python 3.12 preinstalled, nothing else needed) |
| Size | Standard_B1s or B1ms (plenty for 4 players) |
| Authentication | **SSH public key**, username `azureuser` |
| Inbound ports at creation | SSH (22) only |

Keep the downloaded `.pem` private key **outside the repository** (e.g. `~/.ssh/bomberman_key.pem`).
The repo's `.gitignore` ignores `*.pem` and `*.key` so a key can't be committed by accident.

```bash
ssh -i ~/.ssh/bomberman_key.pem azureuser@20.79.184.8
```

### 4.2 Firewall rules (VM → Networking → Add inbound port rule)
| Name | Port | Protocol | Source | Why |
|---|---|---|---|---|
| SSH | 22 | TCP | *My IP address* (recommended) | Administration |
| `Allow-HTTP-80` | 80 | TCP | Any | Let's Encrypt validation + redirect to HTTPS |
| `Allow-HTTPS-443` | 443 | TCP | Any | **The game** (page + `wss://`) |
| `Allow-Bomberman-TCP-8000` | 8000 | TCP | Any | *Optional:* only for the native pygame client (raw TCP). Browsers don't need it. |

*Source port ranges* is always `*` (a client's own port is random). The built-in `DenyAllInBound`
rule blocks everything not explicitly allowed.

### 4.3 DNS name (VM → Overview → DNS name)
DNS name label: `bomberman-arena`, which gives
`bomberman-arena.germanywestcentral.cloudapp.azure.com` → `20.79.184.8`.

### 4.4 Copy the code to the VM
From the project folder on Windows (Git Bash). `git archive` sends only committed files, so no keys,
no local database, no PDFs:

```bash
git archive --format=tar HEAD | ssh -i ~/.ssh/bomberman_key.pem azureuser@20.79.184.8 \
  'mkdir -p ~/bomberman-arena && tar -xf - -C ~/bomberman-arena && mkdir -p ~/bomberman-arena/data'
```

### 4.5 Run the game server as a systemd service
`/etc/systemd/system/bomberman-tcp.service`:

```ini
[Unit]
Description=Bomberman Arena - TCP server
After=network-online.target
Wants=network-online.target

[Service]
User=azureuser
WorkingDirectory=/home/azureuser/bomberman-arena
ExecStart=/usr/bin/python3 tcp_version/server_tcp.py --host 0.0.0.0 --port 8000
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bomberman-tcp
```

- `Restart=on-failure`: if Python crashes, systemd restarts it after 3 s.
- `enable`: starts automatically when the VM boots.
- `PYTHONUNBUFFERED=1`: `print()` output shows up in the logs immediately.
- `NoNewPrivileges` / `PrivateTmp`: small hardening, since the process doesn't need extra rights.

### 4.6 Fix: the VM could not resolve domain names
`apt-get install` failed with `Temporary failure resolving 'archive.ubuntu.com'`. Azure's internal DNS
server (`168.63.129.16`) was timing out, while public DNS worked. We added fallback resolvers:

`/etc/systemd/resolved.conf.d/fallback-dns.conf`:
```ini
[Resolve]
# Azure DNS (168.63.129.16) was timing out; fall back to public resolvers
DNS=168.63.129.16 1.1.1.1 8.8.8.8
```
```bash
sudo systemctl restart systemd-resolved
```

### 4.7 Install nginx and certbot
```bash
sudo apt-get update
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

### 4.8 nginx reverse-proxy config
`/etc/nginx/sites-available/bomberman` (enabled with a symlink in `sites-enabled/`; the default site was removed):

```nginx
# "Connection: upgrade" for WebSocket requests, "close" for normal page/image requests
# (the Python server answers one HTTP request per connection).
map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}

server {
    server_name bomberman-arena.germanywestcentral.cloudapp.azure.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;                          # WebSocket needs HTTP/1.1
        proxy_set_header Upgrade $http_upgrade;          # forward the WebSocket handshake
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;                             # real-time game: send snapshots immediately
        proxy_read_timeout 1h;                           # default 60s would cut quiet WebSockets
        proxy_send_timeout 1h;
    }

    # --- added by certbot ---
    listen 443 ssl;
    listen [::]:443 ssl ipv6only=on;
    ssl_certificate     /etc/letsencrypt/live/bomberman-arena.germanywestcentral.cloudapp.azure.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bomberman-arena.germanywestcentral.cloudapp.azure.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

# --- added by certbot: send http:// visitors to https:// ---
server {
    if ($host = bomberman-arena.germanywestcentral.cloudapp.azure.com) {
        return 301 https://$host$request_uri;
    }
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name bomberman-arena.germanywestcentral.cloudapp.azure.com;
    return 404;
}
```

**Why each WebSocket line matters:**
- A WebSocket starts as a normal HTTP request with `Upgrade: websocket` and `Connection: Upgrade`.
  These are *hop-by-hop* headers: **nginx drops them by default**. Without the two `proxy_set_header`
  lines, the Python server never sees an upgrade request, and the browser's WebSocket fails.
- `proxy_http_version 1.1`: the upgrade mechanism doesn't exist in HTTP/1.0 (nginx's default to upstreams).
- `proxy_read_timeout 1h`: nginx closes a proxied connection after 60 s with no data by default.
- `proxy_buffering off`: deliver each game snapshot immediately instead of collecting a buffer.

### 4.9 Get the certificate
```bash
sudo certbot --nginx -d bomberman-arena.germanywestcentral.cloudapp.azure.com \
  --agree-tos --register-unsafely-without-email --redirect
```

- Let's Encrypt checks that we control the domain by requesting a file over **port 80**, which is why port 80 must be open.
- `--redirect` adds the HTTP → HTTPS redirect.
- `--register-unsafely-without-email` means no email is shared, but also no expiry warnings by email.
- Renewal is automatic (`systemctl status certbot.timer`). The certificate lasts 90 days and is renewed ~30 days before expiry.

**Gotcha we hit:** nginx refused to start:
```
could not build server_names_hash, you should increase server_names_hash_bucket_size: 64
```
The Azure domain name is long (54 characters). Fix in `/etc/nginx/nginx.conf`, inside `http { }`:
```nginx
server_names_hash_bucket_size 128;
```
then `sudo certbot install --nginx --cert-name <domain> --redirect`.

### 4.10 Kernel setting left on (harmless)
`/etc/sysctl.d/60-bomberman-mtu.conf` enables TCP MTU probing (`net.ipv4.tcp_mtu_probing = 1`).
It was added while testing the MTU theory. It didn't fix this problem, but it helps on networks that
really do drop large packets, so it was kept.

---

## 5. Verification

Tested from outside the VM:

| Check | Result |
|---|---|
| `https://<domain>/` | 200, valid certificate |
| `http://<domain>/` | 301 → `https://` |
| Full 3-bot match over `wss://` through nginx (port 443) | 2/2 matches completed, all snapshots received |
| Same over the old direct `:8000` on university Wi-Fi | froze after ~5 s (the original problem) |

The `wss://` test client is a small script that reuses `arena/wsproto.py` to do the browser's handshake
and frame masking, then follows the same protocol as `tools/bots.py`.

**On campus:** open `https://bomberman-arena.germanywestcentral.cloudapp.azure.com` (not the `:8000`
address) and play a match for at least 30 seconds.

---

## 6. Day-to-day operations

```bash
ssh -i ~/.ssh/bomberman_key.pem azureuser@20.79.184.8

sudo systemctl status bomberman-tcp        # is the game server running?
journalctl -u bomberman-tcp -f             # live logs (connections, matches)
sudo systemctl restart bomberman-tcp       # after updating the code
sudo nginx -t && sudo systemctl reload nginx   # after editing nginx config
sudo certbot certificates                  # certificate expiry date
```

**Update the code on the server:** commit locally, re-run the `git archive | ssh` command from 4.4,
then `sudo systemctl restart bomberman-tcp`.

**Save money:** stop (deallocate) the VM in the portal when not in use. The disk and database are kept.

---

## 7. Troubleshooting

| Symptom | Likely cause / check |
|---|---|
| Page doesn't load at all | VM stopped? `systemctl status nginx bomberman-tcp`; NSG rule for 443 present? |
| `502 Bad Gateway` | nginx is up but the game server isn't: `sudo systemctl restart bomberman-tcp` |
| Page loads, "connection lost" immediately | WebSocket headers missing in nginx config (see 4.8) |
| Freezes a few seconds into a match on campus | You used `http://...:8000`. Use the `https://` domain |
| `http://20.79.184.8` shows 404 | Expected: nginx only answers for the domain name now |
| `apt` hangs / "Temporary failure resolving" | DNS fallback (see 4.6) |
| Browser warns about the certificate | Certificate expired? `sudo certbot renew --dry-run` |

## 8. Known limitations
- **The pygame client (`--proto tcp`) can't use port 443.** It speaks raw JSON lines over TCP, not HTTP,
  so nginx can't forward it. It still works on port 8000, but that port is likely blocked on campus.
- **The UDP version isn't deployed** (it would need UDP 9000 open, and university networks often block UDP).
- **Server logs show `127.0.0.1` for browser players**, because they connect through nginx.
  The real IP is available in the `X-Real-IP` header if needed.
