# Deploying crypto-bot on a VPS

A practical guide to running the bot on a Linux VPS under PM2, and accessing
the dashboard securely from anywhere.

---

## 1. Prerequisites

Install these on the VPS (Ubuntu/Debian shown):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
# Node.js + PM2 (PM2 is a Node tool that supervises the Python process)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g pm2
```

## 2. Get the code

**First time:**

```bash
git clone https://github.com/BetterCallDzuks/crypto-bot.git
cd crypto-bot
```

**Updating later:**

```bash
cd crypto-bot
git checkout main
git pull origin main
```

## 3. Install and configure

```bash
./setup.sh            # creates .venv, installs deps, copies .env from template
nano .env             # paste your Binance API key + secret
nano config.yaml      # optional: quote_currency, symbols, leverage, port, ...
```

`.env` is **not** in git (it holds your keys) — you create it on each machine.
`setup.sh` is safe to re-run after every `git pull` to refresh dependencies.

> **Go slow.** For the first run on the VPS keep `exchange.sandbox: true` and
> `trading.dry_run: true`. Confirm it connects to Binance and prices flow,
> then move to low leverage before real funds. See the README safety section.

## 4. Run under PM2

```bash
pm2 start ecosystem.config.js     # start bot + dashboard
pm2 logs crypto-bot               # follow logs
pm2 restart crypto-bot            # after changing symbols or quote currency
pm2 save                          # remember the process list
pm2 startup                       # print a command; run it so PM2 auto-starts on reboot
```

---

## 5. Accessing the dashboard from anywhere

> ### ⚠️ Read this first
> The dashboard has **no login**, and it can **toggle live trading** and change
> risk parameters. Anyone who can open it can move your money. So the rule is:
> **never expose port 4000 directly to the public internet.** By default the
> bot binds to `127.0.0.1` (localhost only) on purpose. Use one of the methods
> below to reach it remotely without leaving it open to the world.

Pick the option that fits how you want to connect. Ordered easiest/safest
first.

### Option A — SSH tunnel (nothing to install on the server)

Best when you connect from a laptop that has an SSH client. Leave the bot on
`127.0.0.1:4000` and forward the port over your existing SSH session:

```bash
# run on your local machine
ssh -L 4000:127.0.0.1:4000 user@YOUR_VPS_IP
```

Then open **http://127.0.0.1:4000** in your local browser. Traffic is encrypted
by SSH, and nothing is exposed publicly. Close the SSH session to end access.

### Option B — Tailscale (best for phone + laptop, "from anywhere")

[Tailscale](https://tailscale.com) puts your VPS and your devices on a private
encrypted network (WireGuard). No public ports, no domain, works from your
phone. This is the simplest way to get true "from anywhere" access safely.

```bash
# on the VPS
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Install the Tailscale app on your phone/laptop and log in with the same
account. Then reach the dashboard at the VPS's Tailscale name/IP, e.g.
`http://YOUR-VPS.tailXXXX.ts.net:4000`.

For this to work the dashboard must listen on the VPS's network interface, not
only localhost. Set it in `config.yaml` and keep the public firewall closed:

```yaml
web:
  host: 0.0.0.0     # listen on all interfaces (Tailscale reaches it)
  port: 4000
```

```bash
# block public access to 4000; Tailscale traffic still gets through
sudo ufw allow ssh
sudo ufw deny 4000
sudo ufw enable
```

(Tailscale connects out to its coordination server and does not need port 4000
opened in the firewall.)

### Option C — Public HTTPS with a password (Caddy reverse proxy)

Use this only if you want a normal `https://bot.yourdomain.com` URL. It adds
**TLS + a password** in front of the dashboard so it isn't wide open. You need
a domain pointing at the VPS.

Install Caddy, then create `/etc/caddy/Caddyfile`:

```
bot.yourdomain.com {
    # generate the hash with:  caddy hash-password
    basicauth {
        admin JDJhJDE0J...your-bcrypt-hash...
    }
    reverse_proxy 127.0.0.1:4000
}
```

```bash
caddy hash-password           # paste the hash into the Caddyfile
sudo systemctl reload caddy
```

Keep the bot on `web.host: 127.0.0.1` so only Caddy can reach it, and firewall
port 4000 shut. Caddy obtains and renews a Let's Encrypt certificate
automatically. Basic-auth is a minimal gate — use a long, unique password, and
prefer Option A or B if you don't specifically need a public URL.

---

## Which should I use?

| Situation                                   | Use        |
|---------------------------------------------|------------|
| Connecting from a laptop with SSH           | Option A   |
| Want easy access from phone + laptop        | Option B (Tailscale) |
| Need a shareable `https://` URL             | Option C (Caddy + auth) |

Whatever you choose, **do not** simply set `host: 0.0.0.0` and open port 4000
in the firewall with no auth — that publishes a trading control panel to the
internet.

## Troubleshooting

- `pm2 logs crypto-bot` — see startup errors and trade activity.
- **"No space left on device"** — clear old logs in `./data/` and PM2 logs
  (`pm2 flush`).
- **Config error on start** — the message names the offending field; fix it in
  `config.yaml` and `pm2 restart crypto-bot`.
- **Network error fetching prices** — the exchange host may be blocked or the
  symbols wrong for your account; verify tradable symbols on Binance and check
  `market.quote_currency` / `market.symbols`.
