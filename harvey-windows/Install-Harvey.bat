@echo off
title Install Harvey
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText('%~f0'); $i=$s.IndexOf('#PS'+'BEGIN'); Invoke-Expression $s.Substring($i)"
echo.
pause
exit /b
#PSBEGIN
# Installs the Harvey sales agent for the current Windows user. No coding needed.
# Everything goes in %LOCALAPPDATA%\Harvey. Safe to run again: it keeps your settings and data.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Harvey version this installer was checked against (github.com/ethanplusai/harvey, MIT).
$commit = '8023a9495eb46d644f03da8c8f4d2b9e41e5836e'
$testMode = ($env:HARVEY_INSTALL_TEST -eq '1')   # used by the automatic check, skips questions
$root = Join-Path $env:LOCALAPPDATA 'Harvey'
$app  = Join-Path $root 'app'
$venv = Join-Path $root 'venv'

function Say($t)  { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Ask($q)  { if ($testMode) { return $true }; $a = Read-Host "$q (Y/n)"; return ($a -eq '' -or $a -match '^[Yy]') }

function Find-Python {
    $candidates = @()
    foreach ($v in '3.13', '3.12', '3.11') {
        try { $p = & py "-$v" -c "import sys;print(sys.executable)" 2>$null; if ($p) { $candidates += $p } } catch {}
    }
    foreach ($dir in 'Python313', 'Python312', 'Python311') {
        $p = Join-Path $env:LOCALAPPDATA "Programs\Python\$dir\python.exe"
        if (Test-Path $p) { $candidates += $p }
    }
    try { $p = (Get-Command python -ErrorAction SilentlyContinue).Source; if ($p) { $candidates += $p } } catch {}
    foreach ($c in $candidates) {
        try {
            $ok = & $c -c "import sys;print(sys.version_info>=(3,11))" 2>$null
            if ($ok -eq 'True') { return $c }
        } catch {}
    }
    return $null
}

Say 'Step 1 of 5: Python'
$python = Find-Python
if (-not $python) {
    Write-Host 'Python 3.11 or newer is needed. Installing it now (about 100 MB).'
    winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
    $python = Find-Python
    if (-not $python) { throw 'Python was installed but could not be found. Close this window, open it again and rerun this installer.' }
}
Write-Host "Using $python"

Say 'Step 2 of 5: Harvey'
New-Item -ItemType Directory -Force -Path $root | Out-Null
$zip = Join-Path $env:TEMP "harvey-$commit.zip"
$tmp = Join-Path $env:TEMP "harvey-$commit"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Invoke-WebRequest "https://github.com/ethanplusai/harvey/archive/$commit.zip" -OutFile $zip
Expand-Archive $zip -DestinationPath $tmp -Force
$src = Join-Path $tmp "harvey-$commit"
New-Item -ItemType Directory -Force -Path $app | Out-Null
# copy the program, but never overwrite your data, passwords file or your trained settings
robocopy $src $app /E /XD data .git /XF .env harvey.local.yaml /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying Harvey failed (robocopy code $LASTEXITCODE)." }
Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue

Say 'Step 3 of 5: Installing Harvey (a few minutes)'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) { & $python -m venv $venv; if ($LASTEXITCODE) { throw 'Could not create the Python environment.' } }
$vpy = Join-Path $venv 'Scripts\python.exe'
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -e $app
if ($LASTEXITCODE) { throw 'Installing Harvey failed. Check your internet connection and run this installer again.' }
$harvey = Join-Path $venv 'Scripts\harvey.exe'
& $harvey --help | Out-Null
if ($LASTEXITCODE) { throw 'Harvey was installed but does not start.' }
# Prepare Harvey's database once, in a single process. If the dashboard and another Harvey command
# create it at the same moment, Harvey can leave it half-upgraded and every start then fails with
# "duplicate column name". Doing it here, alone, avoids that.
Say 'Preparing the database'
Push-Location $app
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$dbCheck = (& $harvey status 2>&1 | Out-String)
$ErrorActionPreference = $prev
Pop-Location
if ($dbCheck -match 'duplicate column|Error running') {
    throw "Harvey's database is damaged (a known start-up clash). If you have no data you need yet: close Harvey, delete the folder $app\data, and run this installer again."
}

Say 'Step 4 of 5: Claude (the AI Harvey uses)'
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) { $alt = Join-Path $env:USERPROFILE '.local\bin\claude.exe'; if (Test-Path $alt) { $claude = $alt } }
if (-not $claude -and -not $testMode) {
    Write-Host 'Harvey thinks through Claude Code, which uses your Claude Pro or Max plan.'
    if (Ask 'Install Claude Code now?') { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression }
    $alt = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path $alt) { $claude = $alt }
}
if ($claude -and -not $testMode) {
    Write-Host 'A new window will open. Type:  claude   then log in in your browser. Close that window when done.'
    Start-Process cmd -ArgumentList '/k', "`"$claude`""
    Read-Host 'Press Enter here after you have logged in to Claude'
} elseif (-not $claude) { Write-Host 'Claude Code is not installed yet. Install it from claude.ai/download, log in, then start Harvey.' }

Say 'Step 5 of 5: Shortcuts'
$starter = Join-Path $root 'Start Harvey.bat'
@"
@echo off
title Harvey
cd /d "$app"
start "Harvey dashboard" /min "$harvey" dashboard
timeout /t 5 >nul
for /f "usebackq delims=" %%u in (``"$harvey" dashboard --url``) do start "" "%%u"
"@ | Set-Content -Path $starter -Encoding ASCII
$setupBat = Join-Path $root 'Harvey setup.bat'
@"
@echo off
title Harvey setup
cd /d "$app"
"$harvey" setup
pause
"@ | Set-Content -Path $setupBat -Encoding ASCII
if (-not $testMode) {
    $sh = New-Object -ComObject WScript.Shell
    foreach ($pair in @(@('Harvey', $starter), @('Harvey setup', $setupBat))) {
        $lnk = $sh.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) "$($pair[0]).lnk"))
        $lnk.TargetPath = $pair[1]; $lnk.WorkingDirectory = $root; $lnk.Save()
    }
}

Write-Host ''
Write-Host 'Harvey is installed.' -ForegroundColor Green
if (Ask 'Run the guided setup now? It asks about your company, product and mailbox') {
    if (-not $testMode) { Push-Location $app; & $harvey setup; Pop-Location }
}
Write-Host ''
Write-Host 'Next time: double-click "Harvey" on your Desktop. It opens the dashboard in your browser.'
Write-Host 'Nothing is emailed until you approve it in the dashboard Outbox.'
