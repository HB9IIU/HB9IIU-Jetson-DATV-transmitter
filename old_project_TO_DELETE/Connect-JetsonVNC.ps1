$ErrorActionPreference = 'Stop'

$ssh = "$env:WINDIR\System32\OpenSSH\ssh.exe"
$key = "$env:USERPROFILE\.ssh\jetson_nano_ed25519"
$viewer = 'C:\Program Files\RealVNC\VNC Viewer\vncviewer.exe'
$target = 'daniel@192.168.0.178'

if (-not (Test-Path -LiteralPath $key)) {
    throw "Jetson SSH key not found: $key"
}

if (-not (Test-Path -LiteralPath $viewer)) {
    throw "RealVNC Viewer not found: $viewer"
}

$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 5901 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    $arguments = @(
        '-N'
        '-L', '5901:localhost:5901'
        '-i', $key
        '-o', 'BatchMode=yes'
        '-o', 'ExitOnForwardFailure=yes'
        '-o', 'ServerAliveInterval=30'
        '-o', 'ServerAliveCountMax=3'
        $target
    )

    Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden

    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 5901 -State Listen -ErrorAction SilentlyContinue) {
            $ready = $true
            break
        }
    }

    if (-not $ready) {
        throw 'Could not establish the encrypted SSH tunnel to the Jetson.'
    }
}

Start-Process -FilePath $viewer -ArgumentList 'localhost:5901'
