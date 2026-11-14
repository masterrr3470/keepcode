# keepcode installer for Windows. No administrator rights needed.
#
#   irm https://raw.githubusercontent.com/masterrr3470/keepcode/main/install.ps1 | iex
#
# Downloads the newest keepcode.exe from GitHub Releases, checks its SHA-256,
# puts it in %LOCALAPPDATA%\Programs\keepcode, adds that folder to your PATH,
# creates Start menu and desktop shortcuts and opens the app. Run it again at any
# time to repair or update.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo = 'masterrr3470/keepcode'
$assetName = 'keepcode-windows-x64.exe'
$dir = Join-Path $env:LOCALAPPDATA 'Programs\keepcode'
$exe = Join-Path $dir 'keepcode.exe'
$headers = @{ 'User-Agent' = 'keepcode-installer'; 'Accept' = 'application/vnd.github+json' }

function Say($text, $color = 'Gray') { Write-Host "  $text" -ForegroundColor $color }

Write-Host ''
Write-Host '  keepcode' -ForegroundColor Cyan -NoNewline
Write-Host '  installer'
Write-Host ''

Say 'Looking for the newest version...'
$release = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers $headers
$asset = $release.assets | Where-Object { $_.name -eq $assetName }
$sums = $release.assets | Where-Object { $_.name -eq 'SHA256SUMS.txt' }
if (-not $asset -or -not $sums) {
    throw "Version $($release.tag_name) has no Windows build yet. Try again in a few minutes."
}

New-Item -ItemType Directory -Force -Path $dir | Out-Null
$download = "$exe.download"
$sumsFile = Join-Path $dir 'SHA256SUMS.txt'
Say "Downloading keepcode $($release.tag_name) ($([math]::Round($asset.size / 1MB, 1)) MB)..."
Invoke-WebRequest $asset.browser_download_url -OutFile $download -UseBasicParsing
Invoke-WebRequest $sums.browser_download_url -OutFile $sumsFile -UseBasicParsing

$line = Get-Content $sumsFile | Where-Object { $_ -match [regex]::Escape($assetName) } | Select-Object -First 1
$expected = ($line -split '\s+')[0].ToLower()
$actual = (Get-FileHash $download -Algorithm SHA256).Hash.ToLower()
Remove-Item $sumsFile -ErrorAction SilentlyContinue
if ($actual -ne $expected) {
    Remove-Item $download -ErrorAction SilentlyContinue
    throw 'The download does not match its checksum, so it was discarded. Try again.'
}
Say 'Checksum verified.' 'DarkGray'

if (Test-Path $exe) {
    # keepcode may be open: a running program can be renamed, not overwritten.
    Move-Item -Force $exe "$exe.old" -ErrorAction SilentlyContinue
}
Move-Item -Force $download $exe

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
if (-not (($userPath -split ';') -contains $dir)) {
    $newPath = (($userPath.TrimEnd(';')), $dir | Where-Object { $_ }) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Say 'Added keepcode to your PATH.' 'DarkGray'
}
$env:Path = "$env:Path;$dir"

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
    if (-not $folder) { continue }
    $link = $shell.CreateShortcut((Join-Path $folder 'keepcode.lnk'))
    $link.TargetPath = $exe
    $link.WorkingDirectory = $env:USERPROFILE
    $link.Description = 'Find every project that is not backed up'
    $link.WindowStyle = 7  # starts minimized, so its console never flashes before the window opens
    $link.Save()
}

Write-Host ''
Write-Host "  keepcode $($release.tag_name) is installed." -ForegroundColor Cyan
Say 'Open it from the desktop or the Start menu. In a terminal, type keepcode.'
Write-Host ''
Say 'Opening keepcode...' 'DarkGray'
Start-Process -FilePath $exe -WorkingDirectory $env:USERPROFILE -WindowStyle Minimized
Write-Host ''
