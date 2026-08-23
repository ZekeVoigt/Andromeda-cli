# ============================================================================
# Andromeda CLI installer — Windows
# ============================================================================
#   iex (irm https://ai-andromeda.com/install.ps1)
#
# The PowerShell counterpart to install.sh. Same layout decision: a checkout
# plus a venv, not a packaged wheel, because the CLI resolves bundled assets
# from its own tree at runtime.
# ============================================================================
$ErrorActionPreference = 'Stop'

# The public distribution repository. It carries the CLI and nothing else.
# ANDROMEDA_REPO_URL points this at a monorepo checkout for internal testing;
# the layout probe below handles both shapes.
$RepoUrl = if ($env:ANDROMEDA_REPO_URL) { $env:ANDROMEDA_REPO_URL } else { 'https://github.com/ZekeVoigt/andromeda-cli.git' }
$Branch  = if ($env:ANDROMEDA_BRANCH)   { $env:ANDROMEDA_BRANCH }   else { 'main' }

# Not '.andromeda': that is the desktop app's data directory.
$Home_    = if ($env:ANDROMEDA_HOME) { $env:ANDROMEDA_HOME } else { Join-Path $HOME '.andromeda-cli' }
$Root     = Join-Path $Home_ 'checkout'
$BinDir   = Join-Path $env:LOCALAPPDATA 'Programs\andromeda'

function Say  ($m) { Write-Host $m }
function Step ($m) { Write-Host "-> $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "OK $m"  -ForegroundColor Green }
function Warn ($m) { Write-Host "!  $m"  -ForegroundColor Yellow }
function Die  ($m) { Write-Host "x  $m"  -ForegroundColor Red; exit 1 }

Say ''
Write-Host 'Andromeda CLI' -ForegroundColor Cyan
Say ''

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Die 'git is required. Install it from https://git-scm.com and re-run.'
}

# Stop uv from picking up config from another profile.
$env:UV_NO_CONFIG = '1'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Step 'Installing uv (Python toolchain manager)'
  try { irm https://astral.sh/uv/install.ps1 | iex } catch { Die 'Could not install uv. See https://astral.sh/uv' }
  $env:Path = "$HOME\.local\bin;$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Die 'uv installed but is not on PATH. Open a new shell and re-run.'
  }
}
Ok 'uv is available'

if (Test-Path (Join-Path $Root '.git')) {
  Step "Updating existing install at $Root"
  git -C $Root fetch --depth 1 origin $Branch  | Out-Null
  git -C $Root reset --hard "origin/$Branch"   | Out-Null
} else {
  Step "Cloning into $Root"
  New-Item -ItemType Directory -Force -Path (Split-Path $Root) | Out-Null
  git clone --depth 1 --branch $Branch $RepoUrl $Root | Out-Null
}

# Two layouts are real: the distribution repo, which *is* the package, and a
# monorepo checkout, where the package is one directory of it. Probe for the
# marker rather than assume, so ANDROMEDA_REPO_URL can point at either.
$CliDir = if (Test-Path (Join-Path $Root 'cli\pyproject.toml')) { Join-Path $Root 'cli' }
          elseif (Test-Path (Join-Path $Root 'pyproject.toml'))  { $Root }
          else { Die 'Checkout has no pyproject.toml - wrong repository or branch?' }

# Same reasoning as the shell installer: git has already moved, so a failure
# here leaves new code against old dependencies. Fail loudly.
Step 'Building the environment'
$VenvPath = Join-Path $CliDir '.venv'
uv venv --python 3.13 $VenvPath | Out-Null
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'
uv pip install --python $VenvPython -e $CliDir | Out-Null
if ($LASTEXITCODE -ne 0) {
  Die 'Dependency install failed. The checkout is updated but not runnable - re-run this installer.'
}
Ok 'Environment ready'

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Shim = Join-Path $BinDir 'andromeda.cmd'
"@echo off`r`n`"$(Join-Path $VenvPath 'Scripts\andromeda.exe')`" %*" | Set-Content -Path $Shim -Encoding ASCII
Ok "Wrote $Shim"

$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($UserPath -notlike "*$BinDir*") {
  [Environment]::SetEnvironmentVariable('Path', "$BinDir;$UserPath", 'User')
  Warn 'Added the install directory to your PATH. Open a new terminal for it to take effect.'
}

Say ''
Ok 'Installed.'
Say ''
Say '  Pair this machine:   andromeda auth login <code>'
Say '  Or bring your own:   $env:OPENROUTER_API_KEY="..."; andromeda config set provider direct'
Say '  Then just:           andromeda'
Say ''
