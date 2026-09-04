#!/usr/bin/env bash
# setup-ubuntu.sh — förbereder en Oracle Cloud-instans (Ubuntu 24.04, aarch64)
# för att köra plattformen.
#
#   scp -i <nyckel> deploy/setup-ubuntu.sh ubuntu@<ip>:~
#   ssh -i <nyckel> ubuntu@<ip> 'bash setup-ubuntu.sh'
#
# Skriptet är idempotent och kan köras om.

set -euo pipefail

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# ── Kontroll ──────────────────────────────────────────────────────────────
log "Systemkontroll"
ARCH="$(uname -m)"
MEM_GB="$(awk '/MemTotal/ {printf "%.1f", $2/1048576}' /proc/meminfo)"
echo "  arkitektur : $ARCH"
echo "  minne      : ${MEM_GB} GB"
echo "  cpu        : $(nproc) kärnor"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "x86_64" ]]; then
    echo "Okänd arkitektur, avbryter." >&2
    exit 1
fi

# ── Paket ─────────────────────────────────────────────────────────────────
log "Uppdaterar paket"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    ca-certificates curl gnupg git ufw netfilter-persistent postgresql-client

# ── Docker ────────────────────────────────────────────────────────────────
# Ubuntus egen docker.io ligger efter. Officiella repot har arm64-byggen och
# compose-plugin, vilket compose-filen förutsätter.
if ! command -v docker >/dev/null 2>&1; then
    log "Installerar Docker"
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo usermod -aG docker "$USER"
    echo "  Docker installerad. Logga ut och in för att slippa sudo."
else
    log "Docker finns redan: $(docker --version)"
fi

# ── Brandvägg ─────────────────────────────────────────────────────────────
# Oracles Ubuntu-images levereras med iptables-regler som släpper igenom
# enbart SSH. Att öppna porten i konsolens Security List räcker INTE — den
# styr molnets nätverk, inte instansens egen brandvägg. Båda måste öppnas.
log "Öppnar portar i instansens brandvägg"
open_port() {
    local port="$1"
    if sudo iptables -C INPUT -m state --state NEW -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
        echo "  port $port redan öppen"
    else
        sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
        echo "  port $port öppnad"
    fi
}
open_port 80
open_port 443
sudo netfilter-persistent save >/dev/null
echo "  regler sparade"

# ── Swap ──────────────────────────────────────────────────────────────────
# Instansen har inget swaputrymme som standard. 2 GB räcker som skyddsnät vid
# indexbygge och laddning; databasen ska ändå ligga i RAM.
if ! swapon --show | grep -q .; then
    log "Skapar 2 GB swap"
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    sudo sysctl -w vm.swappiness=10 >/dev/null
    echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf >/dev/null
else
    log "Swap finns redan"
fi

# ── Tidszon och sammanfattning ────────────────────────────────────────────
sudo timedatectl set-timezone Europe/Stockholm

log "Klart"
cat <<EOF

Nästa steg:

  1. Logga ut och in igen (så att docker fungerar utan sudo).

  2. Öppna portarna i molnets nätverk också:
     Oracle-konsolen -> VCN -> Security Lists -> Default Security List
     -> Add Ingress Rules
        Source 0.0.0.0/0, TCP, destination port 80
        Source 0.0.0.0/0, TCP, destination port 443

  3. Hämta projektet och starta stacken:
     git clone <repo> && cd <repo>
     cp .env.example .env      # fyll i lösenorden
     docker compose up -d
     docker compose run --rm migrate

  4. Ladda exporten (se docs/handover.md avsnitt 3).

Kontrollera med:
  docker --version && docker compose version
  sudo iptables -L INPUT -n --line-numbers | head
EOF
