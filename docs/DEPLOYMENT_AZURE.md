# Deploying the TCP version on Azure (and making it work on the university network)

This document explains how the **TCP version** of Bomberman Arena is hosted on an Azure virtual machine,
why the first setup did **not** work on the university Wi-Fi, how we diagnosed it, and what we changed
so that it does.

**Play:** `python client/pygame_client.py --proto tcp --host bomberman-arena.germanywestcentral.cloudapp.azure.com --port 443`

> **History.** The first deployment served a *browser* client over HTTPS/WebSocket (nginx + Let's Encrypt).
> The project was then changed to use **plain TCP sockets only** (no HTTP, no WebSocket, no browser),
> so nginx now forwards **raw TCP** on port 443 instead (section 3). Sections 2 and 3 keep the story
> of why port 443 is used at all.

---

## 1. Final architecture

```
                   Internet / university Wi-Fi
                              │
       raw TCP, JSON lines    │  port 443
                              ▼
┌──────────────────── Azure VM (Ubuntu 24.04, 20.79.184.8) ────────────────────┐
│                                                                              │
│   nginx  "stream" module  (layer 4: copies bytes, does not read them)        │
│     │                                                                        │
│     │  raw TCP, only inside the VM                                           │
│     ▼                                                                        │
│   python3 tcp_version/server_tcp.py   (port 8000, managed by systemd)        │
│     └── data/arena.db  (SQLite: accounts, scores, match history)             │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

| Component | Role |
|---|---|
| **Azure VM** (Standard B1s/B1ms, Ubuntu Server 24.04 LTS) | A small Linux server with a public IP |
| **Network Security Group (NSG)** | Azure's firewall: decides which ports are reachable from the internet |
| **Azure DNS name label** | Free domain `bomberman-arena.germanywestcentral.cloudapp.azure.com` → `20.79.184.8` |
| **systemd** service `bomberman-tcp` | Starts the game server at boot, restarts it if it crashes |
| **nginx** (`stream` module) | TCP proxy: accepts connections on 443 and pipes them to the game server on 8000 |

The game code does not know about nginx: the client simply connects to port 443 instead of 8000
(`--port 443`), and every byte is forwarded unchanged.

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

## 3. The fix: use port 443

University firewalls almost always allow **port 443**, because blocking it would break the web.

**Version 1 (browser client):** the game was served as a normal HTTPS website, with nginx terminating
TLS (Let's Encrypt certificate) and forwarding WebSocket traffic. With encryption, the firewall could not
see that the connection carried a game stream. This worked on campus.

**Version 2 (current, plain TCP only):** there is no browser and no HTTP anymore, so an HTTP reverse proxy
can't be used. nginx's **`stream` module** works one layer lower (layer 4): it accepts a TCP connection on
443 and copies the bytes to `127.0.0.1:8000` without reading them. The pygame client connects with `--port 443`.

> **Not yet confirmed on campus.** The traffic on 443 is now *unencrypted* JSON. A firewall that only
> filters by port lets it through. One that inspects traffic (DPI) may notice that "port 443" traffic is not
> TLS and cut it. Test a full match on the university Wi-Fi. If it freezes, the next step is TLS on the
> stream (nginx `listen 443 ssl` in the `stream` block, plus `ssl` wrapping in the client).

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
| `Allow-HTTPS-443` | 443 | TCP | Any | **The game** (raw TCP through nginx) |
| `Allow-Bomberman-TCP-8000` | 8000 | TCP | Any | *Optional:* direct access without nginx (blocked on campus) |
| `Allow-HTTP-80` | 80 | TCP | Any | *No longer needed* (was for Let's Encrypt in version 1) |

*Source port ranges* is always `*` (a client's own port is random). The built-in `DenyAllInBound`
rule blocks everything not explicitly allowed.

### 4.3 DNS name (VM → Overview → DNS name)
DNS name label: `bomberman-arena`, which gives
`bomberman-arena.germanywestcentral.cloudapp.azure.com` → `20.79.184.8`.

### 4.4 Copy the code to the VM
From the project folder on Windows (Git Bash). `git archive` sends only committed files, so no keys,
no local database, no PDFs. Move the old code aside first so deleted files don't linger,
but keep `data/` (the database):

```bash
ssh -i ~/.ssh/bomberman_key.pem azureuser@20.79.184.8 \
  'B=~/bomberman-arena-old-$(date +%F); mkdir -p $B; cp -a ~/bomberman-arena/data $B/data-copy;
   find ~/bomberman-arena -mindepth 1 -maxdepth 1 ! -name data -exec mv -t $B {} +'
git archive --format=tar HEAD | ssh -i ~/.ssh/bomberman_key.pem azureuser@20.79.184.8 \
  'mkdir -p ~/bomberman-arena/data && tar -xf - -C ~/bomberman-arena'
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

### 4.7 Install nginx and its stream module
```bash
sudo apt-get update
sudo apt-get install -y nginx libnginx-mod-stream
```
Ubuntu's nginx builds `stream` as a *dynamic module*. `libnginx-mod-stream` installs it and loads it via
`/etc/nginx/modules-enabled/50-mod-stream.conf`.

### 4.8 nginx TCP pass-through config
`/etc/nginx/bomberman-stream.conf`:

```nginx
stream {
    server {
        listen 443;
        listen [::]:443;
        proxy_pass 127.0.0.1:8000;
        proxy_connect_timeout 5s;
        proxy_timeout 1h;          # idle limit; clients send heartbeats, so this is only a safety net
        tcp_nodelay on;            # small real-time messages: no Nagle delay
    }
}
```

It is included at the **top level** of `/etc/nginx/nginx.conf` (last line, outside `http { }`),
because `stream` is a sibling of `http`, not part of it:

```nginx
include /etc/nginx/bomberman-stream.conf;
```

The old HTTPS site (`/etc/nginx/sites-available/bomberman`) is kept for reference but **disabled**
(its symlink in `sites-enabled/` was removed), since only one process can listen on 443.

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4.9 Version 1 only: TLS certificate (no longer used)
Version 1 used `certbot --nginx -d bomberman-arena.germanywestcentral.cloudapp.azure.com` for a
Let's Encrypt certificate, plus `server_names_hash_bucket_size 128;` in `nginx.conf` because the Azure
domain name is long. The certificate is still on the VM in `/etc/letsencrypt/` but nothing uses it now.
It would be reused if TLS is added to the stream (see section 3).

### 4.10 Kernel setting left on (harmless)
`/etc/sysctl.d/60-bomberman-mtu.conf` enables TCP MTU probing (`net.ipv4.tcp_mtu_probing = 1`).
It was added while testing the MTU theory. It didn't fix this problem, but it helps on networks that
really do drop large packets, so it was kept.

---

## 5. Verification

```bash
python tools/bots.py --proto tcp --host bomberman-arena.germanywestcentral.cloudapp.azure.com --port 443 --bots 3
```

| Check | Result |
|---|---|
| Full 3-bot match over raw TCP via nginx :443 (2026-11-12) | completed, 91/91 snapshots per bot, 0 lost, RTT about 40 ms |
| Version 1 (`wss://` on 443) on university Wi-Fi | worked |
| Direct `:8000` on university Wi-Fi | froze after ~5 s (the original problem) |
| Version 2 (raw TCP on 443) on university Wi-Fi | **to be tested** |

---

## 6. Day-to-day operations

```bash
ssh -i ~/.ssh/bomberman_key.pem azureuser@20.79.184.8

sudo systemctl status bomberman-tcp        # is the game server running?
journalctl -u bomberman-tcp -f             # live logs (connections, matches)
sudo systemctl restart bomberman-tcp       # after updating the code
sudo nginx -t && sudo systemctl reload nginx   # after editing nginx config
```

**Update the code on the server:** commit locally, re-run the `git archive | ssh` command from 4.4,
then `sudo systemctl restart bomberman-tcp`.

**Save money:** stop (deallocate) the VM in the portal when not in use. The disk and database are kept.

---

## 7. Troubleshooting

| Symptom | Likely cause / check |
|---|---|
| Client can't connect at all | VM stopped? `systemctl status nginx bomberman-tcp`; NSG rule for 443 present? |
| Connects then drops immediately | nginx is up but the game server isn't: `sudo systemctl restart bomberman-tcp` |
| Freezes a few seconds into a match on campus | Used port 8000? Use `--port 443`. Still freezes on 443 → the firewall inspects traffic; add TLS (section 3) |
| `nginx -t`: `unknown directive "stream"` | `libnginx-mod-stream` not installed (4.7) |
| `nginx -t`: `"stream" directive is not allowed here` | The include is inside `http { }`; it must be at the top level |
| `apt` hangs / "Temporary failure resolving" | DNS fallback (see 4.6) |

## 8. Known limitations
- **Traffic on 443 is unencrypted** (plain JSON lines, including login messages). Passwords are stored hashed
  on the server, but they travel in clear text. TLS on the stream would fix this and help with strict campus firewalls.
- **Server logs show `127.0.0.1` for every player**, because all connections arrive through nginx.
  (nginx's `proxy_protocol` could pass the real IP, but the server would have to parse it.)
- **The UDP version isn't deployed** (it would need UDP 9000 open, and university networks often block UDP).
