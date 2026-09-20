# Deployment — home machine + Tailscale

The running home for this bot is **a computer you own, on your own internet connection**,
reached from anywhere over Tailscale. Not a rented VPS. This document is the runbook for
the developer; the operator who actually uses the bot never sees any of it — they tap one
button in Telegram.

`PLAN.md` §3 Stage 6 is the task list this implements.

---

## 1. Why a home machine

Facebook decides how suspicious a login looks mostly from **where it comes from**. With
2FA switched off on the bot account — a deliberate choice, see `PLAN.md` §8 — a familiar
residential IP is one of the few signals left keeping that account un-challenged. A
datacenter IP discards it at exactly the moment the device is also new.

It also settles three other things at once:

- **No residential proxy to buy.** A home line *is* residential. That was the largest
  recurring cost in the budget.
- **The human is near the keyboard.** Checkpoints are resolved by a person, by design;
  having that person in the same building is worth more than any automation.
- **The GUI stack is free.** A VM capable of running headed Chrome plus a desktop is not
  the cheapest tier; hardware you already own is.

What it costs: **your power and your internet become the product's uptime.** If the client
later needs guaranteed availability, split it — bot and SearXNG on a small VPS, the
Facebook worker at home, talking over Tailscale. Don't build that split now; just don't
design anything that prevents it.

### Pick an Intel machine if you have one

Google does not ship **Chrome for Linux on arm64**. The image therefore pins
`platform: linux/amd64` (see `docker-compose.yml`). On an Intel box that is native. On
Apple Silicon it runs under emulation — workable, but a browser is the worst thing to
emulate, so prefer the Intel machine if you have a choice.

---

## 2. What actually runs

One container. Everything inside it talks over loopback, which is the point:

```
Telegram  ──►  bot  ──────────────►  SearXNG        (127.0.0.1:8888)
                │
                ├──► Playwright ───►  Chrome        (CDP on 127.0.0.1:9222)
                │                      └─ Xvfb display, headed, persistent profile
                │                          └─ x11vnc (127.0.0.1:5900)
                │                              └─ websockify/noVNC (127.0.0.1:6080)
                └──► token gate  ◄── Tailscale ◄── the operator's phone
                     (127.0.0.1:8090)
```

**Nothing but the gate is reachable from outside**, and the gate itself only through
Tailscale. `docker/entrypoint.sh` starts and supervises the lot; if any process dies the
container exits and `restart: unless-stopped` brings it back together.

Chrome's debugging port deserves naming twice: **CDP has no authentication of any kind.**
Anything that reaches port 9222 owns the logged-in browser and its cookies. It is bound to
loopback inside a single container, and `tests/test_gate_hardening.py` fails if that ever
changes.

---

## 3. First run

On the machine that will host it:

```bash
git clone <this repo> ~/REAL-ESTATE-BOT
cd ~/REAL-ESTATE-BOT
make setup          # interactive .env builder; keys never leave the machine
```

Fill in at minimum `TELEGRAM_TOKEN`, an LLM provider, `FACEBOOK_ENABLED=true`,
`FACEBOOK_GROUP_URLS` and `FACEBOOK_ADMIN_TELEGRAM_IDS`. Leave `FACEBOOK_EMAIL` and
`FACEBOOK_PASSWORD` **unset** — see §6.

```bash
docker compose build bot
docker compose up bot
```

Expect the entrypoint to narrate: SearXNG, then Xvfb, Chrome, x11vnc, noVNC, then the bot.
A `startup.ready` line with your bot's username means it is live.

**The Chrome profile and the gate's token file live in `./data`**, bind-mounted from the
host. That is what makes the Facebook login survive `docker compose down`. Back it up and
never commit it — a browser profile contains live session cookies.

---

## 4. Reaching it from anywhere — Tailscale

Install Tailscale on the host machine and on the operator's phone, and sign both into the
same tailnet. Then:

### Preferred: tailnet-private

If the operator's phone has Tailscale, the gate needs no public exposure at all:

```
FACEBOOK_DESKTOP_PUBLIC_BASE=http://<machine-name>:8090
```

The URL only resolves for devices on your tailnet. Nothing is published to the internet,
so there is no address for anyone else to find or scan.

### Fallback: Tailscale Funnel

If installing an app on the operator's phone is not workable, Funnel publishes the gate
over HTTPS:

```bash
tailscale funnel --bg 8090
```

It prints an address like `https://your-machine.your-tailnet.ts.net`. Put that in
`FACEBOOK_DESKTOP_PUBLIC_BASE` (no trailing slash).

**Funnel is genuinely public.** The bot therefore refuses to start unless
`FACEBOOK_DESKTOP_PIN` is also set — as it does for any base another device could
open, including a LAN address. A loopback base is exempt, which is what makes local
development and the first Facebook login straightforward — without it, possession of a forwarded Telegram
message is possession of a browser logged into Facebook. Over HTTPS the PIN cookie is
also marked `Secure` automatically.

Whichever you choose: **never** `tailscale funnel` ports 6080, 5900 or 9222, and never add
a compose mapping for them. The gate is the only intended way in.

---

## 5. Keeping it up

- `restart: unless-stopped` covers crashes and daemon restarts.
- For reboots, make Docker start at login: Docker Desktop → Settings → General → *Start
  Docker Desktop when you sign in*. On a Linux host, `systemctl enable docker`.
- The machine must **not sleep**. On macOS: System Settings → Lock Screen, and
  `caffeinate -s` if needed. A sleeping host is an offline bot and a dead Facebook session.
- Check it is alive: `docker compose ps` and `docker compose logs --tail=50 bot`.

---

## 6. Do not store the Facebook password

`FACEBOOK_EMAIL` and `FACEBOOK_PASSWORD` are optional and should stay unset.

The primary path is a human logging in through the live-view button, and the persistent
profile means that happens rarely. With 2FA off on the bot account, a stored password is
most of what protects it — sitting on the same disk as a browser that is already logged
in. Leaving it out removes that exposure *and* the entire automatic-login branch.

The bot logs a `startup.deployment_posture` warning if it finds one, and starts anyway:
this is a posture preference, not a reason to lock an operator out of their own bot
mid-incident.

---

## 7. Two browsers, on purpose

There are two, and they must never become one:

| | Facebook | Listing portals |
|---|---|---|
| Binary | real Google Chrome | bundled Chromium |
| Profile | persistent, logged in | none, thrown away per run |
| Control | one owner, lock held | concurrent, a few tabs |
| Settings | `FACEBOOK_*` | `PARSER_BROWSER_*` |

Routing an Idealista fetch through the logged-in profile would put the operator's
Facebook identity behind every listing request. The separation is structural — the parser
settings expose no profile directory and no CDP endpoint — and `tests/test_deployment.py`
fails if such an option is ever added.

---

## 8. Verifying the doors are shut

From **another machine on the network**, not the host:

```bash
nmap -p 5900,6080,8090,8888,9222 <host-ip>
```

Every one should be closed or filtered. If any is open, a compose mapping or a firewall
rule is wrong — stop and fix it before the gate is published.

The repository guards the same thing at build time: `make test` fails if any compose
mapping loses its `127.0.0.1:` prefix, if 6080/5900/9222 are ever published, or if the
entrypoint stops binding CDP, x11vnc and websockify to loopback.

---

## 9. When something breaks

| Symptom | Where to look |
|---|---|
| Bot silent in Telegram | `docker compose logs bot`; check `startup.ready` appeared |
| "Open Facebook" link expired | Normal — tokens are short-lived. Tap `/facebook` again. |
| Link opens but the page is blank | noVNC is up but Chrome is not; check the entrypoint logs |
| Searches return nothing from Facebook | `/facebook` → Status. If unhealthy, the watchdog should already have messaged you |
| A group stopped being read | The rechecker messages you on change; it is usually membership |
| Chrome will not start on Apple Silicon | Docker Desktop → Settings → General → *Use Rosetta for x86/amd64 emulation* |

---

## 10. What this replaces

`render.yaml` and the README's Render section remain for the search-only deployment, which
has no browser and no Facebook. They are not the path for the full product: Render cannot
host a long-lived headed Chrome with a persistent profile, and its IPs are the datacenter
addresses §1 is about avoiding.
