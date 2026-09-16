# Finds a working Python 3 for this machine - installing it automatically
# when there is none - and prints the interpreter's full path on its own line.
#
# Called two ways by the .bat scripts:
#   powershell -File ensure_python.ps1              find or install, chatty
#   powershell -File ensure_python.ps1 -NoInstall   find only, quiet
#
# Exit code 0 = a path was printed; 1 = no usable Python and the install
# could not be completed either.
#
# Why this exists: "where python" lies. A PC can have only the "py" launcher
# on PATH, or worse, the Microsoft Store's fake python.exe stub that opens
# the Store instead of running anything. The only honest test is to run the
# candidate and ask it for sys.executable - which is what this does.

param([switch]$NoInstall)

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($name in 'py', 'python', 'python3') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $candidates.Add($cmd.Source) }
    }
    $roots = @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:ProgramFiles\Python313", "$env:ProgramFiles\Python312",
        "$env:ProgramFiles\Python311", "$env:ProgramFiles\Python310",
        'C:\Python313', 'C:\Python312', 'C:\Python311'
    )
    foreach ($root in $roots) {
        if (Test-Path $root) {
            Get-ChildItem -Path $root -Filter python.exe -Recurse -Depth 2 `
                -File -ErrorAction SilentlyContinue |
                ForEach-Object { $candidates.Add($_.FullName) }
        }
    }
    foreach ($candidate in $candidates) {
        $out = $null
        try {
            if ((Split-Path $candidate -Leaf) -ieq 'py.exe') {
                $out = & $candidate -3 -c 'import sys; print(sys.executable)' 2>$null
            } else {
                $out = & $candidate -c 'import sys; print(sys.executable)' 2>$null
            }
        } catch { }
        if ($out) {
            $exe = "$out".Trim()
            if ($exe -and (Test-Path $exe)) { return $exe }
        }
    }
    return $null
}

$found = Find-Python
if ($found) { Write-Output $found; exit 0 }
if ($NoInstall) { exit 1 }

Write-Host ''
Write-Host 'Python is not installed on this PC - setting it up now (one-off).' -ForegroundColor Yellow

# Route 1: winget, when it works.
if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host '  Trying winget...'
    try {
        winget install -e --id Python.Python.3.12 --accept-package-agreements `
            --accept-source-agreements --disable-interactivity | Out-Null
    } catch { }
    $found = Find-Python
    if ($found) { Write-Output $found; exit 0 }
    Write-Host '  winget did not produce a working Python - trying python.org directly.' -ForegroundColor Yellow
}

# Route 2: the official installer straight from python.org. Installs
# per-user (no admin prompt), adds itself to PATH and the py launcher.
$url = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
$exe = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
Write-Host '  Downloading Python 3.12 from python.org (about 25 MB)...'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
} catch {
    Write-Host "  Download failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host '  Install Python yourself from https://www.python.org/downloads/windows/'
    Write-Host '  (tick "Add python.exe to PATH"), then run this again.'
    exit 1
}
Write-Host '  Installing (takes a minute, no clicks needed)...'
Start-Process -FilePath $exe -Wait -ArgumentList `
    '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0'
Remove-Item $exe -Force -ErrorAction SilentlyContinue

$found = Find-Python
if ($found) {
    Write-Host '  Done.' -ForegroundColor Green
    Write-Output $found
    exit 0
}
Write-Host '  The install ran but Python still cannot be found.' -ForegroundColor Red
Write-Host '  Close this window, open a NEW one and try again - a fresh'
Write-Host '  window picks up the updated PATH.'
exit 1
