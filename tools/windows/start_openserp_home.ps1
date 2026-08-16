param(
  [int]$Port = 7000,
  [string]$ContainerName = "gws-openserp-home",
  [string]$Root = "C:\GWS\home-runner"
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Test-OpenSerp {
  try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$Port/health"
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
  } catch { return $false }
}

function Wait-OpenSerp([int]$Seconds = 45) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  do {
    if (Test-OpenSerp) { return $true }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $deadline)
  return $false
}

function Test-DockerDaemon {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
  try { docker info *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

function Start-DockerDesktopIfPresent {
  $candidates = @(
    (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\Docker Desktop.exe'),
    (Join-Path $env:LOCALAPPDATA 'Docker\Docker Desktop.exe')
  ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
  foreach ($exe in $candidates) {
    try {
      Write-Host "OPENSERP_HOME_DOCKER_WAKE exe=$exe"
      Start-Process -FilePath $exe -WindowStyle Hidden -ErrorAction Stop | Out-Null
      return $true
    } catch {
      Write-Warning "Could not launch Docker Desktop from $exe : $($_.Exception.Message)"
    }
  }
  return $false
}

function Ensure-DockerBackend {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }

  if (-not (Test-DockerDaemon)) {
    [void](Start-DockerDesktopIfPresent)
    $deadline = (Get-Date).AddSeconds(75)
    while ((Get-Date) -lt $deadline -and -not (Test-DockerDaemon)) { Start-Sleep -Seconds 3 }
  }
  if (-not (Test-DockerDaemon)) { return $false }

  try {
    $exists = @(docker ps -a --format '{{.Names}}' 2>$null) -contains $ContainerName
    if ($exists) {
      $running = @(docker ps --format '{{.Names}}' 2>$null) -contains $ContainerName
      if (-not $running) {
        Write-Host "OPENSERP_HOME_DOCKER_START container=$ContainerName"
        docker start $ContainerName | Out-Host
      }
      if (Wait-OpenSerp 30) {
        Write-Host "OPENSERP_HOME_OK backend=docker port=$Port container=$ContainerName"
        return $true
      }
      Write-Warning "Existing OpenSERP container is unhealthy; recreating it."
      docker rm -f $ContainerName | Out-Null
    }

    docker pull karust/openserp:latest | Out-Host
    docker run -d `
      --name $ContainerName `
      --restart unless-stopped `
      -p "127.0.0.1:${Port}:7000" `
      karust/openserp:latest `
      serve -a 0.0.0.0 -p 7000 | Out-Host

    if (Wait-OpenSerp 60) {
      Write-Host "OPENSERP_HOME_OK backend=docker port=$Port container=$ContainerName"
      return $true
    }
    try { docker logs --tail 100 $ContainerName | Out-Host } catch {}
  } catch {
    Write-Warning "Docker OpenSERP bootstrap failed: $($_.Exception.Message)"
  }
  return $false
}

function Get-NativeOpenSerp {
  $osDir = Join-Path $Root 'openserp'
  New-Item -ItemType Directory -Force $osDir | Out-Null
  $exe = Get-ChildItem $osDir -Recurse -Filter 'openserp.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($exe) { return $exe.FullName }

  Write-Host 'OPENSERP_HOME_NATIVE_PROVISION=1'
  $rel = Invoke-RestMethod -TimeoutSec 20 -Headers @{ 'User-Agent'='GWS-Home-OpenSERP/2.0' } 'https://api.github.com/repos/karust/openserp/releases/latest'
  $asset = $rel.assets | Where-Object { $_.name -match 'openserp-windows-amd64-.*\.tgz$' } | Select-Object -First 1
  if (-not $asset) { throw 'No Windows amd64 OpenSERP release asset found.' }
  $archive = Join-Path $osDir $asset.name
  Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Headers @{ 'User-Agent'='GWS-Home-OpenSERP/2.0' } -OutFile $archive $asset.browser_download_url
  tar -xzf $archive -C $osDir
  $exe = Get-ChildItem $osDir -Recurse -Filter 'openserp.exe' | Select-Object -First 1
  if (-not $exe) { throw 'Native openserp.exe not found after extraction.' }
  return $exe.FullName
}

function Ensure-NativeBackend {
  try {
    $exe = Get-NativeOpenSerp
    $oldTracking = $env:RUNNER_TRACKING_ID
    $env:RUNNER_TRACKING_ID = ''
    try {
      Write-Host "OPENSERP_HOME_NATIVE_START exe=$exe port=$Port"
      Start-Process -FilePath $exe -ArgumentList @('serve','-a','127.0.0.1','-p',"$Port") -WorkingDirectory (Split-Path $exe -Parent) -WindowStyle Hidden | Out-Null
    } finally {
      $env:RUNNER_TRACKING_ID = $oldTracking
    }
    if (Wait-OpenSerp 45) {
      Write-Host "OPENSERP_HOME_OK backend=native port=$Port"
      return $true
    }
  } catch {
    Write-Warning "Native OpenSERP bootstrap failed: $($_.Exception.Message)"
  }
  return $false
}

if (Test-OpenSerp) {
  Write-Host "OPENSERP_HOME_OK backend=existing port=$Port"
  exit 0
}

if (Ensure-DockerBackend) { exit 0 }
Write-Warning 'Docker backend unavailable; switching to native OpenSERP fallback.'
if (Ensure-NativeBackend) { exit 0 }

throw "OpenSERP failed to become healthy on http://127.0.0.1:$Port/health via Docker or native fallback."
