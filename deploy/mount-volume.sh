#!/usr/bin/env bash
# mount-volume.sh — formaterar och monterar en Oracle-blockvolym för dokumenten.
#
# Kör EFTER att volymen kopplats till instansen i konsolen
# (Attachment type: Paravirtualized).
#
#   sudo bash mount-volume.sh
#
# Skriptet formaterar ALDRIG en disk som redan har ett filsystem. Innehåller
# volymen data monteras den bara. Det gör det säkert att köra om.

set -euo pipefail

MOUNT_POINT="${MOUNT_POINT:-/srv/decisa/documents}"
LABEL="decisa-docs"

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "Kör med sudo." >&2
    exit 1
fi

# ── Hitta volymen ─────────────────────────────────────────────────────────
# Bootvolymen är den som har monterade partitioner. Den vi söker är en disk
# utan monteringspunkt.
log "Söker efter okopplad blockvolym"
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT

CANDIDATES=()
while read -r name type mnt; do
    [[ "$type" == "disk" ]] || continue
    # hoppa över diskar som har monterade partitioner (bootvolymen)
    if lsblk -no MOUNTPOINT "/dev/$name" | grep -q '/'; then
        continue
    fi
    CANDIDATES+=("$name")
done < <(lsblk -rno NAME,TYPE,MOUNTPOINT)

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
    echo
    echo "Hittade ingen ledig blockvolym." >&2
    echo "Kontrollera att volymen är kopplad i konsolen:" >&2
    echo "  Instans -> Attached block volumes -> Attach block volume" >&2
    exit 1
fi
if [[ ${#CANDIDATES[@]} -gt 1 ]]; then
    echo
    echo "Flera lediga diskar hittades: ${CANDIDATES[*]}" >&2
    echo "Ange vilken med: DEVICE=/dev/sdX sudo bash $0" >&2
    [[ -n "${DEVICE:-}" ]] || exit 1
fi

DEV="${DEVICE:-/dev/${CANDIDATES[0]}}"
SIZE="$(lsblk -dno SIZE "$DEV")"
log "Använder $DEV ($SIZE)"

# ── Filsystem ─────────────────────────────────────────────────────────────
EXISTING_FS="$(lsblk -dno FSTYPE "$DEV" || true)"
if [[ -n "$EXISTING_FS" ]]; then
    echo "  Volymen har redan filsystem ($EXISTING_FS) — formaterar INTE."
else
    log "Formaterar $DEV som ext4"
    echo "  Detta raderar allt på $DEV. Ctrl+C inom 10 sekunder för att avbryta."
    sleep 10
    # ext4 utan reserverade block: detta är ett datalager, inte en systemdisk,
    # och 5 % av 100 GB är 5 GB i onödan.
    mkfs.ext4 -m 0 -L "$LABEL" "$DEV"
fi

# ── Montering ─────────────────────────────────────────────────────────────
UUID="$(blkid -s UUID -o value "$DEV")"
mkdir -p "$MOUNT_POINT"

if ! grep -q "$UUID" /etc/fstab; then
    log "Lägger till i /etc/fstab"
    # nofail: instansen ska starta även om volymen inte hunnit kopplas,
    # annars fastnar den i felsäkert läge vid omstart.
    printf 'UUID=%s %s ext4 defaults,nofail,noatime 0 2\n' \
        "$UUID" "$MOUNT_POINT" >> /etc/fstab
else
    echo "  Redan i /etc/fstab"
fi

mountpoint -q "$MOUNT_POINT" || mount "$MOUNT_POINT"

# Ägs av ubuntu så att uppladdning via rsync fungerar utan sudo.
chown -R ubuntu:ubuntu "$MOUNT_POINT"

log "Klart"
df -h "$MOUNT_POINT"
cat <<EOF

Dokumenten laddas upp hit: $MOUNT_POINT

Från din Windows-maskin (Git Bash eller WSL):

  rsync -avz --partial --progress \\
      -e "ssh -i ~/.ssh/decisa-oracle.key" \\
      /d/siris/pdf/ ubuntu@<ip>:$MOUNT_POINT/

Använd rsync, inte FTP eller scp: den kan återupptas efter avbrott och
överför bara det som saknas. 89 GB tar tid, och avbrott är att räkna med.

Kontrollera efteråt att antalet stämmer:

  ssh ubuntu@<ip> "ls $MOUNT_POINT | wc -l"
EOF
