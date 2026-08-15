param(
  [Parameter(Mandatory=$true)][string]$RegistrationToken,
  [string]$RepoUrl = "https://github.com/walidgdg1-ai/evergreenleadminer",
  [string]$Root = "C:\GWS\home-runner",
  [string]$RunnerName = "gws-home-$env:COMPUTERNAME",
  [string]$Labels = "gws-home,gws-residential",
  [int]$OpenSerpPort = 7000
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator. The GitHub runner is installed as a Windows service."
  }
}

function Test-Http([string]$Url, [int]$Timeout = 8) {
  try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec $Timeout $Url
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
  } catch { return $false }
}

function Get-LatestAsset([string]$Repo, [string]$Pattern) {
  $rel = Invoke-RestMethod -Headers @{ 'User-Agent'='GWS-Home-Installer/1.0' } "https://api.github.com/repos/$Repo/releases/latest"
  $asset = $rel.assets | Where-Object { $_.name -match $Pattern } | Select-Object -First 1
  if (-not $asset) { throw "No release asset matching $Pattern in $Repo latest release" }
  return $asset
}

function Ensure-OpenSerp {
  param([string]$Base, [int]$Port)
  if (Test-Http "http://127.0.0.1:$Port/health") {
    Write-Host "OPENSERP_HOME_OK existing port=$Port"
    return
  }

  $docker = Get-Command docker -ErrorAction SilentlyContinue
  if ($docker) {
    try {
      docker info *> $null
      $exists = docker ps -a --format '{{.Names}}' | Where-Object { $_ -eq 'gws-openserp-home' }
      if ($exists) { docker rm -f gws-openserp-home | Out-Null }
      docker pull karust/openserp:latest | Out-Host
      docker run -d --name gws-openserp-home --restart unless-stopped -p "127.0.0.1:${Port}:7000" karust/openserp:latest serve -a 0.0.0.0 -p 7000 | Out-Host
      $deadline=(Get-Date).AddSeconds(60)
      while ((Get-Date) -lt $deadline) {
        Start-Sleep 2
        if (Test-Http "http://127.0.0.1:$Port/health") {
          Write-Host "OPENSERP_HOME_OK docker port=$Port"
          return
        }
      }
    } catch {
      Write-Warning "Docker OpenSERP failed; falling back to native binary: $($_.Exception.Message)"
    }
  }

  $osDir = Join-Path $Base 'openserp'
  New-Item -ItemType Directory -Force $osDir | Out-Null
  $asset = Get-LatestAsset 'karust/openserp' 'openserp-windows-amd64-.*\.tgz$'
  $archive = Join-Path $osDir $asset.name
  Invoke-WebRequest -UseBasicParsing -Headers @{ 'User-Agent'='GWS-Home-Installer/1.0' } -OutFile $archive $asset.browser_download_url
  tar -xzf $archive -C $osDir
  $exe = Get-ChildItem $osDir -Recurse -Filter 'openserp.exe' | Select-Object -First 1
  if (-not $exe) { throw "Native openserp.exe not found after extraction" }

  $taskName='GWS OpenSERP Home'
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  $args="serve -a 127.0.0.1 -p $Port"
  $action=New-ScheduledTaskAction -Execute $exe.FullName -Argument $args -WorkingDirectory $exe.DirectoryName
  $trigger=New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $principal=New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal | Out-Null
  Start-ScheduledTask -TaskName $taskName

  $deadline=(Get-Date).AddSeconds(60)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep 2
    if (Test-Http "http://127.0.0.1:$Port/health") {
      Write-Host "OPENSERP_HOME_OK native-scheduled-task port=$Port"
      return
    }
  }
  throw "OpenSERP failed to become healthy on localhost:$Port"
}

function Ensure-Runner {
  param([string]$Base,[string]$Url,[string]$Token,[string]$Name,[string]$RunnerLabels)
  $runnerDir=Join-Path $Base 'actions-runner'
  New-Item -ItemType Directory -Force $runnerDir | Out-Null

  if (Test-Path (Join-Path $runnerDir '.runner')) {
    Write-Host "GWS_HOME_RUNNER_ALREADY_CONFIGURED dir=$runnerDir"
    $svc=Get-Service | Where-Object { $_.Name -like 'actions.runner.*' -and $_.Status -ne 'Running' } | Select-Object -First 1
    if ($svc) { Start-Service $svc.Name }
    return
  }

  $asset = Get-LatestAsset 'actions/runner' 'actions-runner-win-x64-.*\.zip$'
  $zip=Join-Path $Base $asset.name
  Invoke-WebRequest -UseBasicParsing -Headers @{ 'User-Agent'='GWS-Home-Installer/1.0' } -OutFile $zip $asset.browser_download_url
  Expand-Archive -Force $zip $runnerDir

  Push-Location $runnerDir
  try {
    & .\config.cmd --unattended --url $Url --token $Token --name $Name --labels $RunnerLabels --work _work --runasservice
    if ($LASTEXITCODE -ne 0) { throw "config.cmd failed with exit code $LASTEXITCODE" }
  } finally { Pop-Location }

  $svc=Get-Service | Where-Object { $_.Name -like 'actions.runner.*' } | Select-Object -First 1
  if (-not $svc) { throw "GitHub Actions runner service not found after configuration" }
  Set-Service -Name $svc.Name -StartupType Automatic
  if ($svc.Status -ne 'Running') { Start-Service $svc.Name }
  Write-Host "GWS_HOME_RUNNER_OK service=$($svc.Name) name=$Name labels=$RunnerLabels"
}

Assert-Admin
New-Item -ItemType Directory -Force $Root | Out-Null
Ensure-OpenSerp -Base $Root -Port $OpenSerpPort
Ensure-Runner -Base $Root -Url $RepoUrl -Token $RegistrationToken -Name $RunnerName -RunnerLabels $Labels

try {
  $ip=(Invoke-RestMethod -TimeoutSec 10 'https://api.ipify.org?format=json').ip
} catch { $ip='UNAVAILABLE' }
Write-Host "GWS_HOME_READY=1"
Write-Host "GWS_HOME_EGRESS_IP=$ip"
Write-Host "GWS_HOME_OPENSERP=http://127.0.0.1:$OpenSerpPort"
Write-Host "GWS_HOME_RUNNER_LABEL=gws-home"
