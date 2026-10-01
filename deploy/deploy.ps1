# Sync D:\fanless dan D:\ht\test ke ThinkCentre lewat SSH/rsync, dengan struktur
# relatif yang sama (~/fanless dan ~/ht/test) supaya path "context: ../ht/test"
# di docker-compose.yml tetap valid di remote.
#
# Prasyarat: SSH ke ThinkCentre sudah jalan (lihat blocker koneksi LAN di README
# root proyek ini). rsync dijalankan lewat WSL jika tersedia, fallback ke scp.

param(
    [string]$RemoteHost = "CHANGE_ME.local",
    [string]$RemoteUser = "CHANGE_ME"
)

$ErrorActionPreference = "Stop"

if ($RemoteHost -eq "CHANGE_ME.local" -or $RemoteUser -eq "CHANGE_ME") {
    Write-Error "Edit dulu `$RemoteHost dan `$RemoteUser di deploy.ps1 (atau pass sebagai -RemoteHost/-RemoteUser), sesuai IP/hostname ThinkCentre setelah SSH-nya jalan."
    exit 1
}

$RemoteTarget = "${RemoteUser}@${RemoteHost}"

Write-Host "Menyiapkan direktori remote..."
ssh $RemoteTarget "mkdir -p ~/fanless ~/ht/test"

$useWsl = Get-Command wsl -ErrorAction SilentlyContinue

if ($useWsl) {
    Write-Host "Sync via rsync (WSL)..."
    $fanlessPath = (wsl wslpath -a 'D:\fanless').Trim()
    $htTestPath = (wsl wslpath -a 'D:\ht\test').Trim()
    wsl rsync -avz --exclude '.env' --exclude 'mail-collector/config.json' "$fanlessPath/" "${RemoteTarget}:~/fanless/"
    wsl rsync -avz --exclude 'data.db*' --exclude '.git' "$htTestPath/" "${RemoteTarget}:~/ht/test/"
} else {
    Write-Host "rsync/WSL tidak ditemukan, fallback ke scp (lebih lambat, tanpa exclude otomatis)."
    Write-Host "Pastikan .env dan mail-collector/config.json TIDAK ikut ke-copy kalau pakai cara ini secara manual."
    scp -r "D:\fanless\*" "${RemoteTarget}:~/fanless/"
    scp -r "D:\ht\test\*" "${RemoteTarget}:~/ht/test/"
}

Write-Host ""
Write-Host "Selesai sync. Lanjut di ThinkCentre:"
Write-Host "  ssh $RemoteTarget"
Write-Host "  cd ~/fanless && docker compose up -d --build"
