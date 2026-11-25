# pogo installer for Windows
#
#   irm https://raw.githubusercontent.com/kharshit801/pogo/main/install.ps1 | iex
#
# Installs pogo into %USERPROFILE%\.pogo and puts the `pogo` command on your PATH.
# Run it again to update. Needs Python 3.9+ (it offers to install Python with winget).
# No admin rights needed. No git needed.
#
# (This file is plain ASCII on purpose: Windows PowerShell 5.1 misreads UTF-8 scripts.)

function Install-Pogo {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is far faster without its progress bar

    $repo   = 'kharshit801/pogo'
    $branch = if ($env:POGO_BRANCH) { $env:POGO_BRANCH } else { 'main' }
    $root   = Join-Path $env:USERPROFILE '.pogo'
    $app    = Join-Path $root 'app'
    $bin    = Join-Path $root 'bin'

    function Say([string]$text)  { Write-Host $text -ForegroundColor Cyan }
    function Warn([string]$text) { Write-Host $text -ForegroundColor Yellow }
    function Fail([string]$text) { Write-Host "error: $text" -ForegroundColor Red }

    # --- pogo's logo, the same art as on pogo's home screen (made by scripts/logo_blocks.py) ---
    # This file must stay ASCII, so # ^ _ stand for the full, upper-half and lower-half blocks.
    $logo = @(
        ' ^#####################_',
        '  ^#####################',
        '    ^^##################',
        '              _########',
        '             _########',
        '            #######^^',
        '  _########',
        ' _########',
        '########^'
    )
    Write-Host ''
    foreach ($row in $logo) {
        Write-Host ($row.Replace('#', [string][char]0x2588).Replace('^', [string][char]0x2580).Replace('_', [string][char]0x2584))
    }
    Write-Host ''

    # GitHub needs TLS 1.2, which older Windows PowerShell doesn't turn on by itself.
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }

    # --- 1. Python 3.9 or newer ---------------------------------------------------------------
    # The Microsoft Store "python" placeholder only opens the Store, so every candidate is
    # actually run and must report a real version.
    function Find-Python {
        $candidates = @()
        if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@('py', '-3') }
        foreach ($name in @('python', 'python3')) {
            if (Get-Command $name -ErrorAction SilentlyContinue) { $candidates += ,@($name) }
        }
        $local = Join-Path $env:LOCALAPPDATA 'Programs\Python'
        if (Test-Path $local) {
            Get-ChildItem $local -Directory -Filter 'Python3*' | Sort-Object Name -Descending | ForEach-Object {
                $exe = Join-Path $_.FullName 'python.exe'
                if (Test-Path $exe) { $candidates += ,@($exe) }
            }
        }
        foreach ($candidate in $candidates) {
            $exe = $candidate[0]
            $extra = @()
            if ($candidate.Count -gt 1) { $extra = @($candidate[1..($candidate.Count - 1)]) }
            try {
                $output = @(& $exe @extra -c 'import sys; print(sys.executable); print(int(sys.version_info >= (3, 9)))' 2>$null)
                if ($LASTEXITCODE -eq 0 -and $output.Count -ge 2 -and "$($output[1])".Trim() -eq '1') {
                    return "$($output[0])".Trim()
                }
            } catch { }
        }
        return $null
    }

    $python = Find-Python
    if (-not $python) {
        Warn "pogo needs Python 3.9 or newer, and it isn't installed."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $answer = Read-Host 'Install Python 3.12 now with winget? [Y/n]'
            if ($answer -eq '' -or $answer -match '^[Yy]') {
                winget install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements
                # winget doesn't update this window's PATH, so read it again
                $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
                $python = Find-Python
            }
        }
        if (-not $python) {
            Fail 'Install Python from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this installer again.'
            return
        }
    }
    Say "Using Python: $python"

    # --- 2. pogo itself ----------------------------------------------------------------------------
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('pogo-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temp | Out-Null
    try {
        if ($env:POGO_SOURCE) {
            Say "Copying pogo from $env:POGO_SOURCE"
            $source = $env:POGO_SOURCE
        } else {
            Say "Downloading pogo ($branch) from GitHub"
            $zip = Join-Path $temp 'pogo.zip'
            Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$repo/archive/refs/heads/$branch.zip" -OutFile $zip
            Expand-Archive -Path $zip -DestinationPath $temp -Force
            $source = (Get-ChildItem $temp -Directory | Select-Object -First 1).FullName
        }
        if (-not (Test-Path (Join-Path $source 'pogo\cli.py'))) {
            Fail "the download doesn't look like pogo ($source)"
            return
        }
        New-Item -ItemType Directory -Force -Path $root | Out-Null
        if (Test-Path $app) {
            try {
                Remove-Item $app -Recurse -Force
            } catch {
                Fail 'pogo is running. Run "pogo off", then run this installer again.'
                return
            }
        }
        New-Item -ItemType Directory -Path $app | Out-Null
        foreach ($item in @('pogo', 'bin', 'README.md', 'LICENSE')) {
            Copy-Item -Path (Join-Path $source $item) -Destination $app -Recurse -Force
        }
    } catch {
        Fail "couldn't download pogo: $($_.Exception.Message)"
        Warn 'If this PC reaches the internet only through a proxy, run the installer while connected'
        Warn 'some other way (for example a phone hotspot), or with Proxifier still running.'
        return
    } finally {
        Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
    }

    # --- 3. the `pogo` command --------------------------------------------------------------------
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $pythonRef = $python
    foreach ($var in @('LOCALAPPDATA', 'USERPROFILE')) {
        # %VARIABLE% paths keep the file plain ASCII, even for user names with accents
        $value = [Environment]::GetEnvironmentVariable($var)
        if ($value -and $pythonRef.StartsWith($value, [StringComparison]::OrdinalIgnoreCase)) {
            $pythonRef = "%$var%" + $pythonRef.Substring($value.Length)
            break
        }
    }
    if ($pythonRef -match '[^\x00-\x7F]') {
        Warn "Python is installed in a folder with non-English characters: $python"
        Warn 'If pogo doesn''t start, reinstall Python for "just me" in its default folder.'
    }
    $shim = "@echo off`r`n`"$pythonRef`" `"%USERPROFILE%\.pogo\app\bin\pogo`" %*`r`n"
    [IO.File]::WriteAllText((Join-Path $bin 'pogo.cmd'), $shim, [Text.Encoding]::ASCII)

    # --- 4. PATH, for this user only ---------------------------------------------------------------
    # Edited in the registry directly: `setx` would cut a long PATH at 1024 characters.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    $current = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $entries = @($current -split ';' | Where-Object { $_ -ne '' })
    $binRef = '%USERPROFILE%\.pogo\bin'
    if (-not ($entries | Where-Object { $_ -ieq $binRef -or $_ -ieq $bin })) {
        $key.SetValue('Path', (($entries + $binRef) -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
        # tell Explorer and new terminals that the environment changed
        [Environment]::SetEnvironmentVariable('POGO_INSTALLER', '1', 'User')
        [Environment]::SetEnvironmentVariable('POGO_INSTALLER', $null, 'User')
    }
    $key.Close()
    if (-not (($env:Path -split ';') -contains $bin)) { $env:Path = "$env:Path;$bin" }

    # --- 5. check it runs --------------------------------------------------------------------------
    $version = & (Join-Path $bin 'pogo.cmd') --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Fail "pogo didn't start: $version"
        return
    }
    Write-Host ''
    Write-Host "$version is installed." -ForegroundColor Green
    Say 'Open a new terminal window and run:  pogo'
    Write-Host '(It works in this window already. If a new window says "pogo is not recognized", restart Windows Terminal.)'
}

Install-Pogo
