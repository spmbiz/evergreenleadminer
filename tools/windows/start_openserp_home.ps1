param(
  [int]$Port = 7000,
  [string]$ContainerName = "gws-openserp-home"
)
$ErrorActionPreference = 'Stop'

function Test-OpenSerp {
  try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$Port/health"
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
  } catch { return $false }
}

if (Test-OpenSerp) {
  Write-Host "OPENSERP_HOME_OK existing port=$Port"
  exit 0
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "Docker CLI not found. Install/start Docker Desktop, then rerun."
}

# Remove a stale container with the same name, if present.
$exists = docker ps -a --format '{{.Names}}' | Where-Object { $_ -eq $ContainerName }
if ($exists) {
  docker rm -f $ContainerName | Out-Null
}

docker pull karust/openserp:latest | Out-Host

docker run -d `
  --name $ContainerName `
  --restart unless-stopped `
  -p "127.0.0.1:${Port}:7000" `
  karust/openserp:latest `
  serve -a 0.0.0.0 -p 7000 | Out-Host

$deadline = (Get-Date).AddSeconds(45)
do {
  Start-Sleep -Seconds 2
  if (Test-OpenSerp) {
    Write-Host "OPENSERP_HOME_OK started port=$Port container=$ContainerName"
    exit 0
  }
} while ((Get-Date) -lt $deadline)

docker logs --tail 100 $ContainerName | Out-Host
throw "OpenSERP failed health check on localhost:$Port"
