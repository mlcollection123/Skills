# Harvey on a Synology DS224+ (always on, no PC needed)

This runs the Harvey sales agent ([ethanplusai/harvey](https://github.com/ethanplusai/harvey), MIT) on your NAS in Container Manager.
You open its dashboard from your PC through a private SSH tunnel. Nothing is exposed to the internet.

**Status: not tested on a real Synology.** The compose file passes `docker compose config`, and I checked Harvey's own code for the points below, but I could not build the image or run a NAS here. Do the first run with care and send me any error text.

## What you need
- DS224+ with DSM 7.2 and the **Container Manager** package (Package Center).
- Your admin login for the NAS and its IP address (DSM: Control Panel > Network).
- A Claude Pro or Max plan, and a mailbox to send from (a separate sending domain, warmed up).
- A Windows PC with PowerShell (Windows 10 and 11 include the `ssh` command).
- About 3 GB of free space. The first build downloads a lot (it includes a web browser) and can take 15 to 30 minutes. The DS224+ ships with 2 GB RAM, which is tight but workable. Watch Resource Monitor.

## Step 1: turn on SSH (once)
DSM > Control Panel > Terminal & SNMP > tick **Enable SSH service** > Apply. You can turn it off again later.

## Step 2: prepare the folders
In PowerShell on your PC (replace `ADMIN` and `NAS-IP`):
```
ssh ADMIN@NAS-IP
```
Then on the NAS (paste each line; `sudo` asks for your password):
```
sudo mkdir -p /volume1/docker/harvey/app /volume1/docker/harvey/claude
cd /volume1/docker/harvey
sudo curl -L -o harvey.tar.gz https://github.com/ethanplusai/harvey/archive/8023a9495eb46d644f03da8c8f4d2b9e41e5836e.tar.gz
sudo tar -xzf harvey.tar.gz --strip-components=1 -C app
sudo rm harvey.tar.gz
sudo chown -R 1000:1000 app claude
```
That installs a pinned, checked version of Harvey. If `/volume1` is not your volume name, use yours.

Now put `docker-compose.yml` from this folder into `/volume1/docker/harvey/` (DSM File Station > docker > harvey > Upload).
Open it and change `TZ: UTC` to your time zone, for example `Europe/Nicosia`, in both places.

## Step 3: build (slow, once)
```
cd /volume1/docker/harvey
sudo docker compose build
```
If it says `compose` is not a command, use `sudo docker-compose build`.

## Step 4: log in to Claude (once)
```
sudo docker compose run --rm -it harvey claude login
```
It prints a link. Open it on your PC, log in, copy the code it shows and paste it back into the NAS window.
Check it works: `sudo docker compose run --rm harvey claude -p "say hi"`.
If login will not stick, run `claude setup-token` on your PC, then add the token as `CLAUDE_CODE_OAUTH_TOKEN=...` to `app/.env` later (Harvey passes that variable through to Claude).

## Step 5: guided setup
```
sudo docker compose run --rm -it harvey python -m harvey setup
```
It asks about your company, product and mailbox. **For the mailbox, choose SMTP/IMAP with an app password** (Google Workspace app password, or any mail provider). Gmail "sign in with Google" and Microsoft 365 sign-in open a browser on the machine running Harvey, which a NAS does not have.
If you prefer Gmail sign-in: install Harvey on your PC with the Windows installer, run its Gmail authorization there, then copy the file `data\gmail_token.json` from that PC into `/volume1/docker/harvey/app/data/` on the NAS.

Then teach it your product and choose what a good prospect is:
```
sudo docker compose run --rm -it harvey python -m harvey train https://your-company.com
sudo docker compose run --rm -it harvey python -m harvey signals --confirm free
```
Set your physical mailing address in `app/harvey.local.yaml` (or via the dashboard Settings) before the first campaign. CAN-SPAM requires it.

## Step 6: start it for good
```
sudo docker compose up -d
```
Both containers restart by themselves after a power cut or NAS reboot.
Check: `sudo docker compose ps` (both should say running) and `sudo docker logs --tail 40 harvey`.

## Step 7: open the dashboard from your PC
Keep this PowerShell window open (it is the tunnel):
```
ssh -L 5555:127.0.0.1:5555 ADMIN@NAS-IP
```
In a second PowerShell window, get your private link:
```
ssh -t ADMIN@NAS-IP "sudo docker exec harvey-dashboard python -m harvey dashboard --url"
```
Paste the link it prints (it starts with `http://127.0.0.1:5555/#token=`) into your browser. Do not open any other address.

Approve emails in the **Outbox**. Nothing sends without your approval unless you switch that off.

## Everyday use
- Pause everything: dashboard > Controls, or `sudo docker compose stop`. Resume with `sudo docker compose start`.
- Back up `/volume1/docker/harvey/app/data` (database, token) and `app/.env` with Hyper Backup.
- Do not press Start in the dashboard. The `harvey` container already runs the agent.
- Do not forward port 5555 on your router and do not put the dashboard behind the Synology reverse proxy.
- Updating Harvey is manual on purpose: stop it, replace `app` with a newer archive (keep `app/data`, `app/.env`, `app/harvey.local.yaml`), then `sudo docker compose build` and `up -d`.

## Why it is built this way
- The whole `app` folder is mounted over `/app`. Harvey saves settings by renaming a temp file over the old one, and Docker cannot do that to a single mounted file.
- Host networking is used so the loopback-only dashboard is reachable through the SSH tunnel.
- The mailbox, warm-up and sending rules in Harvey's own README still apply. Read its "Legal and deliverability" section first.
