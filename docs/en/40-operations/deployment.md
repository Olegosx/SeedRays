[Index](../index.md) · [Monitoring](monitoring.md) · [Русская версия](../../ru/40-operations/deployment.md)

# Deployment

Deploying the gateway to a production server: installation, the first start, the
systemd service, the publishing schemes and updates. The gateway is one Python
process ([ADR-0003](../20-architecture/decisions/0003-single-process-supervised.md))
that serves the HTTP API, the static frontend and runs the watcher; all its state
lives in one data directory (SQLite in WAL mode, the security journal).

**The scope of trust first**: the online part is watch-only
([ADR-0002](../20-architecture/decisions/0002-watch-only-online-part.md)) — the server
holds public xpubs only and cannot spend funds. A server compromise still exposes
user emails, addresses, balances and the panel — treat it as a production system.

## Requirements

- Linux server; Python **3.13 or newer** (`requires-python` of the package).
- Outbound HTTPS access: to the TRON provider (TronGrid) and to the mail service
  (Resend, [ADR-0020](../20-architecture/decisions/0020-mail-provider.md)).
- Inbound HTTPS: **through a reverse proxy** — see "Publishing schemes" below. The
  gateway process itself does not terminate TLS in the current version, and session
  cookies are marked `Secure`, so browsers will not keep them over plain HTTP
  (localhost excepted); a production deployment without HTTPS is not functional.
- Disk: the databases grow with users and transaction history; the security journal
  is capped by its rotation settings (default ≤ ~1.1 GB worst case).

## Installation

```bash
sudo useradd --system --home /var/lib/seedrays --create-home seedrays
sudo git clone git@github.com:Olegosx/SeedRays.git /opt/seedrays
cd /opt/seedrays/backend
sudo -u seedrays python3 -m venv .venv
sudo -u seedrays .venv/bin/pip install -e .
```

The editable install (`-e`) keeps the package running from the repository checkout —
this is also how the process finds the `frontend/` directory next to it. With a
regular (non-editable) install the static files must be pointed to explicitly via
`SEEDRAYS_FRONTEND_DIR`.

The data directory must belong to the service user and not be world-readable
(it holds the databases and the security journal):

```bash
sudo install -d -o seedrays -g seedrays -m 700 /var/lib/seedrays
```

## Environment Variables

The deployment layer of [ADR-0016](../20-architecture/decisions/0016-config-layers.md);
everything else is a registry setting managed from the operator panel.

| Variable | Required | Meaning |
|----------|----------|---------|
| `SEEDRAYS_DATA_DIR` | yes | The data directory: the registry database, per-user databases, the deleted-user archive (`archive/`), `logs/security.log`. |
| `SEEDRAYS_BIND` | no | API bind address, `host:port`; default `127.0.0.1:8080`. Keep it on localhost — the reverse proxy is the public face. |
| `SEEDRAYS_FRONTEND_DIR` | no | Static frontend directory; by default the `frontend/` directory of the repository checkout is served. |

## First Start

1. **Create the operator** (there is no operator registration —
   [ADR-0004](../20-architecture/decisions/0004-two-api-groups.md)):

   ```bash
   cd /opt/seedrays/backend
   sudo -u seedrays SEEDRAYS_DATA_DIR=/var/lib/seedrays \
       .venv/bin/python -m seedrays.cli operator-create --login admin
   ```

   The password is asked interactively (hidden input). Database migrations run
   automatically on every service start — no separate migration step exists.

2. **Start the service** (see the systemd unit below) and sign in to the panel at
   `https://your-domain/operator-login.html`.

3. **Fill the gateway settings** on the panel's settings page:
   - `gateway.base_url` — the public URL of the gateway; links in emails are built
     **only** from this value (the Host header is never trusted). Without it the
     configured mail sender stays disabled.
   - Mail: the Resend API key and the sender address (`mail.from`). Note Resend's
     own restriction: until your sending domain is verified there, messages are
     delivered only to the mailbox of the Resend account owner.
   - TRON provider: the TronGrid API key and, when needed, the request rate.
   - `gateway.trusted_proxies` — see "Publishing schemes".
   - The security-journal rotation, the watcher interval/overlap — when the
     defaults do not fit.
   - Token contracts to watch: the `watcher.contracts.<network>` setting (a JSON
     list of contract addresses) is deliberately not on the panel and is entered
     manually — the owner's decision.
4. **Verify**: register a cabinet user, attach a wallet, watch the watcher status
   block on the panel (see [Monitoring](monitoring.md)).

## The systemd Service

`/etc/systemd/system/seedrays.service`:

```ini
[Unit]
Description=SeedRays crypto payment gateway
After=network-online.target
Wants=network-online.target

[Service]
User=seedrays
Group=seedrays
WorkingDirectory=/opt/seedrays/backend
Environment=SEEDRAYS_DATA_DIR=/var/lib/seedrays
Environment=SEEDRAYS_BIND=127.0.0.1:8080
ExecStart=/opt/seedrays/backend/.venv/bin/python -m seedrays.cli serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now seedrays
```

Component failures inside the process (the API server, the watcher) are restarted
by the gateway's own supervisor without touching the other component; `Restart=always`
covers the whole-process death. `SIGTERM` (what `systemctl stop` sends) is a graceful
shutdown: open API connections finish, the watcher is cancelled between passes.

## Publishing Schemes

The gateway process listens on localhost; a reverse proxy terminates TLS and
forwards requests. The proxy is also where the visitor's real address is preserved:
list every trusted intermediary in the `gateway.trusted_proxies` setting (IPs and
CIDR ranges, comma-separated; applied on restart) — the gateway then resolves the
client address from `X-Forwarded-For`, but only on connections arriving from a
listed proxy, so the header cannot be spoofed from outside. Without this, the
brute-force brake and the security journal
([ADR-0023](../20-architecture/decisions/0023-security-journal.md)) see the proxy's
address for everyone.

### nginx

```nginx
server {
	listen 443 ssl http2;
	server_name gateway.example.com;

	ssl_certificate     /etc/letsencrypt/live/gateway.example.com/fullchain.pem;
	ssl_certificate_key /etc/letsencrypt/live/gateway.example.com/privkey.pem;

	location / {
		proxy_pass http://127.0.0.1:8080;
		proxy_set_header Host $host;
		proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
		proxy_set_header X-Forwarded-Proto $scheme;
	}
}

server {
	listen 80;
	server_name gateway.example.com;
	return 301 https://$host$request_uri;
}
```

The certificate: `certbot --nginx -d gateway.example.com` (Let's Encrypt; renewal is
installed automatically). The proxy runs on the same machine, so the default trust
(`127.0.0.1`) already covers it — `gateway.trusted_proxies` may stay empty; a proxy
on another machine must be listed there by its address.

### Apache

Modules: `proxy`, `proxy_http`, `ssl`, `headers` (`a2enmod proxy proxy_http ssl headers`).

```apache
<VirtualHost *:443>
	ServerName gateway.example.com
	SSLEngine on
	SSLCertificateFile /etc/letsencrypt/live/gateway.example.com/fullchain.pem
	SSLCertificateKeyFile /etc/letsencrypt/live/gateway.example.com/privkey.pem

	ProxyPreserveHost On
	ProxyPass / http://127.0.0.1:8080/
	ProxyPassReverse / http://127.0.0.1:8080/
	RequestHeader set X-Forwarded-Proto "https"
</VirtualHost>
```

`mod_proxy_http` adds `X-Forwarded-For` on its own; `X-Forwarded-Proto` must be set
explicitly (above). The certificate: `certbot --apache -d gateway.example.com`.

### Behind Cloudflare

Cloudflare is always an intermediary: connections reach your server from Cloudflare's
addresses, and the visitor's IP travels in the forwarded headers. Two additions to
either scheme above:

- In the Cloudflare dashboard use the SSL/TLS mode **Full (strict)** — the origin
  still needs its own valid certificate (Let's Encrypt or a Cloudflare Origin CA
  certificate); anything weaker leaves the Cloudflare-to-origin leg open.
- Add the official Cloudflare ranges (published at <https://www.cloudflare.com/ips/>)
  to `gateway.trusted_proxies`, keeping the local proxy too, e.g.:
  `127.0.0.1, 173.245.48.0/20, 103.21.244.0/22, …` — the header chain is resolved
  right to left across all trusted hops, so "Cloudflare → local nginx → gateway"
  yields the real visitor address. Cloudflare's ranges change rarely but do change —
  re-check them on updates.
- Ideally the origin accepts port 443 only from Cloudflare ranges (a firewall rule):
  otherwise an attacker who learns the origin IP connects directly.

### Restricting the Operator Panel

The operator routes live on the same port as the cabinet. When the panel should not
be reachable from the open internet, restrict it at the proxy — e.g. nginx:

```nginx
	location ~ ^/(v1/operator/|operator-) {
		allow 203.0.113.10;   # the administrator's addresses
		deny all;
		proxy_pass http://127.0.0.1:8080;
		proxy_set_header Host $host;
		proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
		proxy_set_header X-Forwarded-Proto $scheme;
	}
```

## Updates

```bash
cd /opt/seedrays
sudo -u seedrays git pull
sudo -u seedrays backend/.venv/bin/pip install -e ./backend
sudo systemctl restart seedrays
```

Database migrations run automatically at service start. Settings marked "applied
after a restart" (trusted proxies, journal rotation) pick up their values here too.

## Backup

What to back up: the entire data directory — the registry database, the per-user
databases, the deleted-user archive (`archive/`,
[ADR-0024](../20-architecture/decisions/0024-user-deletion-archive.md)) and, if
desired, the security journal. The seed phrase is **not** part of
any backup: the gateway never stores it ([ADR-0002](../20-architecture/decisions/0002-watch-only-online-part.md));
losing the server loses no funds, and balances are recomputable from the chain by a
fresh install with the same xpubs.

The databases are SQLite in WAL mode; a plain file copy of a live database can catch
it mid-write. Two safe options:

```bash
# 1. Cold copy — stop, copy, start:
sudo systemctl stop seedrays && cp -a /var/lib/seedrays /backup/seedrays-$(date +%F) && sudo systemctl start seedrays

# 2. Online copy of one database with SQLite's own backup command:
sqlite3 /var/lib/seedrays/registry.db ".backup /backup/registry.db"
```

Restore is the reverse: stop the service, put the directory back, start.

## Related

- [Monitoring](monitoring.md)
- [Architecture Overview](../20-architecture/overview.md)
- [ADR-0016: Configuration Layers](../20-architecture/decisions/0016-config-layers.md)
- [ADR-0023: Security Event Journal](../20-architecture/decisions/0023-security-journal.md)
- [Operator Panel Scenarios](../50-frontend/operator-panel.md)
- [Threat Model](../30-security/threat-model.md)
