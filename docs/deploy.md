# Running the paper-trading bot

Step by step, from zero to a bot running on a small server. Everything here uses **fake money**
on Binance's testnet. Nothing in this guide can touch real funds.

What you'll end up with: a server that runs `python live.py --once` 5 minutes past every hour.
Once a day, just after 00:00 UTC, the bot trades if the strategy wants a different position.
You get a Telegram message for every trade, every problem, and a daily report.

## 1. Get testnet API keys (5 minutes)

1. Go to <https://testnet.binance.vision> and click **Log In with GitHub**.
2. Click **Generate HMAC_SHA256 Key**, give it any name.
3. Copy the **API Key** and **Secret Key** somewhere safe right away. The secret is shown once.

Don't paste these into chats or commit them anywhere. They go only in the `.env` file on the server.

The testnet is wiped from time to time (roughly monthly). When that happens the bot notices
that its balances changed and **halts**. See "Testnet reset" below.

## 2. Create a Telegram bot (5 minutes)

1. In Telegram, open a chat with **@BotFather**, send `/newbot`, and follow the prompts.
   It gives you a **token** like `123456:ABC-DEF...`.
2. Open a chat with your new bot and send it any message (e.g. "hi").
3. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` (with your token in
   place of `<TOKEN>`). Find `"chat":{"id":123456789` — that number is your **chat id**.

The bot only *sends* messages. It doesn't read commands.

## 3. Set up a heartbeat check (3 minutes)

If the server dies, the bot can't tell you. A free <https://healthchecks.io> account can:

1. Sign up, then **Add Check**. Set **Period: 1 hour**, **Grace: 30 minutes**.
2. Copy the ping URL (`https://hc-ping.com/...`).
3. Under **Integrations**, add email and/or Telegram so it can reach you.

If no ping arrives for 90 minutes, healthchecks.io alerts you.

## 4. Rent a small server

Any provider works. The smallest Linux server is plenty (1 CPU, 1–2 GB RAM, about $4–6/month),
e.g. Hetzner CX22, DigitalOcean's basic droplet, or Vultr.

- Image: **Ubuntu 24.04**.
- Region: **Europe** (e.g. Germany or Finland). Avoid US regions: Binance blocks US IP addresses.
- Log-in: an SSH key if the provider offers it (safer than a password).

## 5. Install the bot on the server

Log in (`ssh root@<server-ip>`), then:

```bash
# A separate user for the bot, and the tools it needs
adduser --disabled-password --gecos "" cryptobot
apt update && apt install -y python3-venv git

# Become that user and install the code
sudo -iu cryptobot
git clone <your-github-repo-url> cryptobot    # private repo: use a GitHub access token as the password
cd cryptobot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q                            # all tests should pass

# Settings and secrets
cp .env.example .env
nano .env                                      # fill in keys, Telegram, healthcheck URL
chmod 600 .env                                 # only this user can read it

# Test everything without trading: keys, prices, Telegram, heartbeat
.venv/bin/python live.py --check
```

`--check` should print your testnet balances and today's target, and you should get a
"Telegram works" message. If it fails, the error says what's missing.

Then start paper trading:

```bash
.venv/bin/python live.py --init     # records starting balances; the bot manages ALLOCATED_USDT of them
exit                                # back to root
```

## 6. Run it every hour

```bash
cp /home/cryptobot/cryptobot/deploy/cryptobot.service /home/cryptobot/cryptobot/deploy/cryptobot.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cryptobot.timer

systemctl list-timers cryptobot.timer      # shows the next run
systemctl start cryptobot.service          # run one tick now, to see it work
journalctl -u cryptobot -n 50              # the bot's log
```

The first tick after `--init` trades right away if the strategy wants a position. After that,
it trades only just after 00:00 UTC.

## Day to day

Check the position any time:

```bash
sudo -iu cryptobot
cd cryptobot && .venv/bin/python live.py --status
```

### What the Telegram messages mean

| Message | What happened | What to do |
|---|---|---|
| `Daily ...: BUY/SELL/HOLD` | The once-a-day decision, with equity and the shadow ledgers | Nothing. Read it if curious |
| `Warning: down 5% today` | Equity fell 5% since the first check today | Nothing. It's informational |
| `HALTED, position kept: down 10% today` | Daily loss limit hit. No new orders; coin is kept | Look at the market. When ready: `live.py --resume` |
| `HALTED ... drawdown -40%` | Worse than anything in the backtest | Stop and think. This is outside what was tested |
| `HALTED ... balances differ` | Exchange balances don't match the bot's records | Usually a testnet reset (below). Otherwise investigate before resuming |
| `HALTED ... orders already tried` | Two order attempts failed in one day | Check `journalctl -u cryptobot`, then `--resume` |
| `Testnet price ... away from the real market` | Testnet price is unrealistic right now | Nothing. It retries every hour |
| `stale data` | Binance hasn't published yesterday's candle | Nothing, unless it lasts all day |
| `ERROR in live.py` | Something crashed (often a network blip) | Nothing if it's once. The next hour retries. If it repeats, check the log |
| `Recovered order ... after a crash` | It crashed mid-order and confirmed the order did go through | Nothing. This is the safety net working |
| healthchecks.io "DOWN" | The bot stopped running | `systemctl status cryptobot.timer`, `journalctl -u cryptobot` |

### Kill switch (sell everything and stop)

Any of these works:

```bash
sudo -iu cryptobot
cd cryptobot && .venv/bin/python live.py --kill    # immediately
touch /home/cryptobot/cryptobot/data/KILL          # at the next hourly tick
```

To restart afterwards: `rm data/KILL`, then `.venv/bin/python live.py --resume`.

### Testnet reset

When Binance wipes the testnet, the bot halts with "balances differ". Then:

1. Log in at testnet.binance.vision again. If your key no longer works, generate a new one
   and update `.env`.
2. `.venv/bin/python live.py --init --force` (starts a fresh paper record from now).

Note the reset date: the paper-trading record restarts there.

### Updating the code

```bash
sudo -iu cryptobot
cd cryptobot && git pull && .venv/bin/pip install -r requirements.txt && .venv/bin/pytest -q
```

The timer picks up the new code at the next tick.

## Safety drills (needed for the go-live criteria)

`results/FINDINGS.md` requires each safety mechanism to be triggered on purpose once. On the
testnet this costs nothing:

1. **Kill switch:** while the bot holds coin, run `live.py --kill`. Check it sold, that the
   Telegram message arrived, and that `--status` shows it stopped. Then `rm data/KILL` and `--resume`.
2. **Daily-loss halt:** while holding coin, add `HARD_DAILY_LOSS=-0.0001` to `.env`. At the
   first hourly tick where the price is below where the day started, it halts. Check that it
   kept the coin. Remove the line and `--resume`.
3. **Drawdown halt:** same with `MAX_DRAWDOWN=-0.0001`, after equity has dipped below its peak.
   Remove the line and `--resume`.
4. **Dead-man's switch:** `systemctl stop cryptobot.timer`, wait for the healthchecks.io
   alert, then `systemctl start cryptobot.timer`.

Write the date of each drill in your notes.

## Where things are

| Path | What |
|---|---|
| `.env` | Keys and settings (secret, never committed) |
| `data/live.sqlite` | Every decision, order and hourly equity sample |
| `data/KILL` | If present, the bot sells and stays stopped |
| `journalctl -u cryptobot` | The log |
