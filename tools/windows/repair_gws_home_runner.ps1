param(
  [Parameter(Mandatory=$true)][string]$RegistrationToken,
  [string]$RepoUrl = "https://github.com/walidgdg1-ai/evergreenleadminer",
  [string]$Root = "C:\GWS\home-runner",
  [string]$RunnerName = "gws-home-$env:COMPUTERNAME",
  [string]$Labels = "gws-home,gws-residential",
  [int]$OpenSerpPort = 7000
)

$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$RegistrationToken=$RegistrationToken.Trim()
if ([string]::IsNullOrWhiteSpace($RegistrationToken)) { throw 'Empty registration token' }

function Assert-Admin {
  $id=[Security.Principal.WindowsIdentity]::GetCurrent()
  $p=New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run PowerShell as Administrator.'
  }
}

function Test-Http([string]$Url,[int]$Timeout=8) {
  try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec $Timeout $Url; return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) }
  catch { return $false }
}

function Get-LatestAsset([string]$Repo,[string]$Pattern) {
  $rel=Invoke-RestMethod -Headers @{'User-Agent'='GWS-Home-Repair/1.0'} "https://api.github.com/repos/$Repo/releases/latest"
  $asset=$rel.assets | Where-Object { $_.name -match $Pattern } | Select-Object -First 1
  if (-not $asset) { throw "No latest release asset matching $Pattern in $Repo" }
  return $asset
}

function Remove-BrokenRunnerService {
  $svcs=Get-CimInstance Win32_Service | Where-Object { $_.Name -like 'actions.runner.walidgdg1-ai-evergreenleadminer.gws-home-*' }
  foreach ($svc in $svcs) {
    Write-Host "GWS_HOME_REPAIR removing stale service=$($svc.Name) state=$($svc.State)"
    try { Stop-Service -Name $svc.Name -Force -ErrorAction SilentlyContinue } catch {}
    Start-Sleep -Seconds 1
    & sc.exe delete $svc.Name | Out-Host
  }
  Start-Sleep -Seconds 2
}

function Fresh-RunnerDirectory([string]$Base) {
  $runnerDir=Join-Path $Base 'actions-runner'
  if (Test-Path $runnerDir) {
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup=Join-Path $Base "actions-runner-broken-$stamp"
    Write-Host "GWS_HOME_REPAIR backing up stale runner to $backup"
    Move-Item -Force $runnerDir $backup
  }
  New-Item -ItemType Directory -Force $runnerDir | Out-Null
  return $runnerDir
}

function Install-FreshRunner([string]$Base,[string]$Url,[string]$Token,[string]$Name,[string]$RunnerLabels) {
  $runnerDir=Fresh-RunnerDirectory $Base
  $asset=Get-LatestAsset 'actions/runner' 'actions-runner-win-x64-.*\.zip$'
  $zip=Join-Path $Base $asset.name
  Write-Host "GWS_HOME_REPAIR downloading $($asset.name)"
  Invoke-WebRequest -UseBasicParsing -Headers @{'User-Agent'='GWS-Home-Repair/1.0'} -OutFile $zip $asset.browser_download_url
  Expand-Archive -Force $zip $runnerDir

  & icacls $runnerDir /grant '*S-1-5-20:(OI)(CI)F' /T /C | Out-Null

  Push-Location $runnerDir
  try {
    & .\config.cmd --unattended --replace --url $Url --token $Token --name $Name --labels $RunnerLabels --work _work --runasservice
    if ($LASTEXITCODE -ne 0) { throw "config.cmd failed with exit code $LASTEXITCODE" }
  } finally { Pop-Location }

  $svc=Get-Service | Where-Object { $_.Name -like 'actions.runner.walidgdg1-ai-evergreenleadminer.gws-home-*' } | Select-Object -First 1
  if (-not $svc) { throw 'Runner service not found after fresh configuration' }
  Set-Service -Name $svc.Name -StartupType Automatic
  if ($svc.Status -ne 'Running') {
    try { Start-Service -Name $svc.Name -ErrorAction Stop }
    catch {
      Write-Host 'GWS_HOME_REPAIR_SERVICE_START_FAILED'
      Write-Host "SERVICE=$($svc.Name)"
      & sc.exe qc $svc.Name | Out-Host
      $diag=Get-ChildItem (Join-Path $runnerDir '_diag') -Filter 'Runner_*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
      if ($diag) { Write-Host "LAST_DIAG=$($diag.FullName)"; Get-Content $diag.FullName -Tail 80 | Out-Host }
      throw
    }
  }
  $svc=Get-Service -Name $svc.Name
  Write-Host "GWS_HOME_RUNNER_OK service=$($svc.Name) status=$($svc.Status) name=$Name labels=$RunnerLabels"
}

Assert-Admin
New-Item -ItemType Directory -Force $Root | Out-Null
if (-not (Test-Http "http://127.0.0.1:$OpenSerpPort/health")) {
  $bootstrap=Join-Path $PSScriptRoot 'start_openserp_home.ps1'
  if (-not (Test-Path $bootstrap)) { throw "OpenSERP bootstrap not found at $bootstrap" }
  Write-Host 'GWS_HOME_REPAIR reviving OpenSERP before runner repair'
  & $bootstrap -Port $OpenSerpPort -Root $Root
}
if (-not (Test-Http "http://127.0.0.1:$OpenSerpPort/health")) {
  throw "OpenSERP is still unhealthy on http://127.0.0.1:$OpenSerpPort/health after bootstrap."
}
Write-Host "OPENSERP_HOME_OK existing port=$OpenSerpPort"
Remove-BrokenRunnerService
Install-FreshRunner -Base $Root -Url $RepoUrl -Token $RegistrationToken -Name $RunnerName -RunnerLabels $Labels
try { $ip=(Invoke-RestMethod -TimeoutSec 10 'https://api.ipify.org?format=json').ip } catch { $ip='UNAVAILABLE' }
Write-Host 'GWS_HOME_READY=1'
Write-Host "GWS_HOME_EGRESS_IP=$ip"
Write-Host "GWS_HOME_OPENSERP=http://127.0.0.1:$OpenSerpPort"
Write-Host 'GWS_HOME_RUNNER_LABEL=gws-home'
