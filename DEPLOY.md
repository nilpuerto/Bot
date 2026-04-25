# Deploying Prym Signals on Oracle Cloud (Always Free, 24/7)

End-to-end guide to run the bot forever, for free, without keeping your PC on.

**What you get:**
- 1 ARM VM (2 vCPU / 12 GB RAM — more than enough) in Oracle Cloud **Always Free** tier
- Docker + docker-compose running the bot on boot
- Auto-restart if it ever crashes
- Logs you can inspect via SSH whenever you want
- Cost: **0€ forever** (free tier has no time limit, unlike AWS 12-month offers)

**Requirements:**
- An Oracle Cloud account (signup below) — needs a credit card for identity
  verification only. Stay on the Always-Free resources and you will never be
  charged. Oracle explicitly does not auto-upgrade without consent.
- SSH basics.

---

## 1. Create the Oracle Cloud account

1. Go to https://signup.cloud.oracle.com/ → fill in the form.
2. Choose a **home region close to Europe** (e.g. `Frankfurt`, `Madrid`, `Paris`)
   — the Always Free pool is shared per-region; pick a less saturated one if
   the first won't give you a VM.
3. Verify email, enter the card, finish signup. Login to the console.

---

## 2. Create the Always-Free VM

1. Console menu → **Compute** → **Instances** → **Create instance**.
2. Name: `prym-bot`.
3. **Image**: change to **Canonical Ubuntu 24.04** (Minimal works).
4. **Shape**: click Change shape → **Ampere** → `VM.Standard.A1.Flex` →
   set **OCPU = 2**, **Memory = 12 GB**. *(Always-Free ceiling is 4 OCPU / 24 GB
   total across all A1 instances; one `2/12` VM fits comfortably.)*
5. **Networking**: accept defaults (new VCN + public subnet + public IPv4).
6. **SSH keys**: choose **Generate a key pair for me** → **Save Private Key**
   to your PC. You'll need that `.key` file to SSH in.
7. **Create**. Wait ~1 min until state becomes `Running` and note down the
   **Public IPv4** address.

---

## 3. Open SSH to the VM

From a Windows PowerShell (or WSL / Git-Bash):

```powershell
# Move the key where you want it and lock down permissions
icacls "C:\path\to\ssh-key.key" /inheritance:r /grant:r "$($env:USERNAME):R"

# Connect
ssh -i C:\path\to\ssh-key.key ubuntu@<PUBLIC_IP>
```

If you get a timeout, open port 22 in the VCN: VCN → Default Security List → add
an ingress rule for `Source CIDR 0.0.0.0/0`, `TCP` destination port `22`. (22
should already be open by default with the Ubuntu image, but double-check.)

---

## 4. Install Docker on the VM

Once SSH'd in:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-v2 git

# Run docker without sudo
sudo usermod -aG docker $USER
newgrp docker

docker --version
docker compose version
```

---

## 5. Clone the repo and drop in your `.env`

On the VM:

```bash
cd ~
git clone https://github.com/<your-fork>/prym.git
cd prym
```

*(If your repo is private, create a deploy key or use a personal access token
when cloning.)*

Create the `.env` on the VM — the safest way is to copy your local file over:

From your Windows PC (new terminal):
```powershell
scp -i C:\path\to\ssh-key.key C:\xampp\htdocs\prym\.env ubuntu@<PUBLIC_IP>:~/prym/.env
```

Verify permissions on the VM:
```bash
chmod 600 .env
```

---

## 6. First-time DB setup (once)

```bash
docker compose run --rm prym-bot python -m scripts.init_db
docker compose run --rm prym-bot python -m scripts.seed_top_traders
```

---

## 7. Start the bot

```bash
docker compose up -d
docker compose logs -f --tail=50
```

You should see lines like `orchestrator_started`, `news_fetched`, …
Send `/start` to your bot on Telegram to confirm everything is wired.

---

## 8. Auto-start on VM reboot

`docker compose up -d` plus `restart: unless-stopped` in
`docker-compose.yml` already handle this: if the VM reboots, Docker starts on
boot, and the container comes back up automatically. No systemd unit needed.

If you want absolute confidence:
```bash
sudo systemctl enable docker
```

---

## 9. Firewall hardening (optional but recommended)

The bot only needs outbound traffic. Close everything except SSH on the VCN:

* VCN → Default Security List → delete any ingress rule besides port 22.
* Oracle's Ubuntu images also use `iptables`; confirm nothing else is open:
  ```bash
  sudo iptables -L INPUT -n --line-numbers
  ```

---

## 10. Day-to-day operations

```bash
# Tail logs
docker compose logs -f

# Restart (after a code update)
git pull
docker compose build
docker compose up -d

# Stop / start
docker compose down
docker compose up -d

# Disk usage
docker system df
```

---

## 11. Updating the bot

```bash
cd ~/prym
git pull
docker compose build
docker compose up -d
```

That's it. The bot is up forever, you can shut your PC, and it keeps running.
If Oracle ever flags your VM for idleness (they sometimes reclaim always-free
resources that have been *completely* idle for 7 days), don't worry — a bot
that polls RSS every 45 s is never idle.

---

## Troubleshooting

- `permission denied (publickey)` on SSH → the `.key` file has wrong
  permissions or you specified the wrong user (`ubuntu` for the Ubuntu image).
- `docker: command not found` after `usermod -aG docker` → run `newgrp docker`
  or reopen the SSH session.
- Bot exits immediately on startup → `docker compose logs` and check for
  `TELEGRAM_BOT_TOKEN is not set` or asyncpg errors. Most common is a missing
  or mis-URL-encoded Supabase password.
- Telegram shows "Bot isn't responding" → `docker compose logs | grep telegram`
  and confirm the bot registered handlers without errors.
