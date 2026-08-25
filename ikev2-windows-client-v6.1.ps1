#requires -Version 5.1

$ErrorActionPreference = "Stop"

$APP_VERSION = "6.1.0"
$DEFAULT_PROXY_HOST = "10.254.254.1"
$DEFAULT_PROXY_PORT = 1080
$STATE_ROOT = Join-Path $env:ProgramData "IKEv2-Windows-VPN-Utility"
$PROFILE_STATE_DIR = Join-Path $STATE_ROOT "profiles"

function Write-Step {
    param([string]$Message)
    Write-Host "[+] $Message" -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Message)
    Write-Host "[i] $Message" -ForegroundColor Gray
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[!] $Message" -ForegroundColor Yellow
}

function Pause-Menu {
    Write-Host ""
    Read-Host "Press Enter to continue"
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Ensure-Administrator {
    if (Test-IsAdministrator) {
        return
    }

    Write-Host "Administrator permission is required. Opening the UAC prompt..." -ForegroundColor Yellow

    try {
        $powershell = (Get-Process -Id $PID).Path
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        Start-Process -FilePath $powershell -ArgumentList $arguments -Verb RunAs
        exit
    }
    catch {
        Write-Host ""
        Write-Host "[!] Administrator permission was not granted." -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 1
    }
}

function Get-CaCertificateFile {
    param([switch]$AllowMissing)

    $certificateFiles = @(
        Get-ChildItem -LiteralPath $PSScriptRoot -Filter "*.cer" -File -ErrorAction SilentlyContinue
    )

    if ($certificateFiles.Count -eq 0) {
        if ($AllowMissing) {
            return $null
        }

        throw "No .cer certificate was found next to this script."
    }

    $preferred = @(
        $certificateFiles | Where-Object { $_.Name -ieq "ca-cert.cer" }
    )

    if ($preferred.Count -eq 1) {
        return $preferred[0]
    }

    if ($certificateFiles.Count -eq 1) {
        return $certificateFiles[0]
    }

    throw "More than one .cer file was found. Keep only the VPN CA certificate next to this script, or name it ca-cert.cer."
}

function Ensure-CaTrusted {
    param($CertificateFile)

    Write-Step "Checking the VPN CA certificate..."

    try {
        if (-not $CertificateFile) {
            $CertificateFile = Get-CaCertificateFile
        }

        $ca = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
            $CertificateFile.FullName
        )
    }
    catch {
        throw "The VPN CA certificate could not be loaded: $($_.Exception.Message)"
    }

    $trustedCa = Get-ChildItem Cert:\LocalMachine\Root |
        Where-Object { $_.Thumbprint -eq $ca.Thumbprint }

    if ($trustedCa) {
        Write-Info "The VPN CA certificate is already trusted in LocalMachine\Root."
    }
    else {
        Write-Step "Importing the VPN CA certificate into LocalMachine\Root..."

        try {
            Import-Certificate `
                -FilePath $CertificateFile.FullName `
                -CertStoreLocation "Cert:\LocalMachine\Root" | Out-Null
        }
        catch {
            throw "Failed to import the VPN CA certificate into LocalMachine\Root."
        }

        $verified = Get-ChildItem Cert:\LocalMachine\Root |
            Where-Object { $_.Thumbprint -eq $ca.Thumbprint }

        if (-not $verified) {
            throw "The CA import command completed, but the certificate was not found in LocalMachine\Root."
        }

        Write-Host "VPN CA certificate imported successfully." -ForegroundColor Green
    }

    return [PSCustomObject]@{
        File       = $CertificateFile.FullName
        Name       = $CertificateFile.Name
        Subject    = $ca.Subject
        Thumbprint = $ca.Thumbprint
    }
}

function Test-IPv4Address {
    param([string]$Address)

    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed)) {
        return $false
    }

    return $parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork
}

function Test-TcpPort {
    param([string]$Port)

    $number = 0
    if (-not [int]::TryParse($Port, [ref]$number)) {
        return $false
    }

    return ($number -ge 1 -and $number -le 65535)
}

function Test-SafeServerIdentity {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt 253) {
        return $false
    }

    if (Test-IPv4Address $Value) {
        return $true
    }

    if ($Value -match '^[0-9.]+$' -or $Value -notmatch '^[A-Za-z0-9.-]+$') {
        return $false
    }

    if ($Value.StartsWith(".") -or $Value.EndsWith(".") -or
        $Value.Contains("..") -or $Value.Contains(".-") -or $Value.Contains("-.")) {
        return $false
    }

    foreach ($label in $Value.Split('.')) {
        if ($label.Length -lt 1 -or $label.Length -gt 63 -or
            $label -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$') {
            return $false
        }
    }

    return $true
}

function Get-RequiredIkevProperty {
    param(
        $Object,
        [string]$Name
    )

    if ($null -eq $Object -or $Object -is [string] -or $Object -is [System.Array] -or
        -not ($Object.PSObject.Properties.Name -ccontains $Name)) {
        throw "This is not a supported IKEv profile."
    }

    return $Object.PSObject.Properties[$Name].Value
}

function Get-RequiredIkevString {
    param(
        $Object,
        [string]$Name
    )

    $value = Get-RequiredIkevProperty -Object $Object -Name $Name
    if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value) -or
        [regex]::IsMatch($value, '[\x00-\x1F\x7F]')) {
        throw "This is not a supported IKEv profile."
    }

    return [string]$value
}

function Read-IkevProfile {
    param([string]$Path)

    try {
        $file = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
    catch {
        throw "The .ikev profile path must be a readable regular file."
    }

    if ($file -isnot [System.IO.FileInfo]) {
        throw "The .ikev profile path must be a readable regular file."
    }
    if ($file.Length -gt 1MB) {
        throw "The .ikev profile is unexpectedly large."
    }

    try {
        $jsonText = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop
        $profile = $jsonText | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "This is not a supported IKEv profile."
    }

    $format = Get-RequiredIkevString -Object $profile -Name "format"
    if ($format -cne "ikev-profile") {
        throw "This is not a supported IKEv profile."
    }

    $version = Get-RequiredIkevProperty -Object $profile -Name "version"
    if (($version -isnot [int]) -and ($version -isnot [long])) {
        throw "Unsupported .ikev format version."
    }
    if ($version -gt 1) {
        throw "This profile uses .ikev format version $version.`nWindows client v$APP_VERSION supports version 1 only."
    }
    if ($version -ne 1) {
        throw "Unsupported .ikev format version: $version.`nWindows client v$APP_VERSION supports version 1 only."
    }

    $name = Get-RequiredIkevString -Object $profile -Name "name"
    if ($name.Length -gt 128) {
        throw "The .ikev profile name is invalid."
    }

    $server = Get-RequiredIkevString -Object $profile -Name "server"
    $remoteId = Get-RequiredIkevString -Object $profile -Name "remote_id"
    if (-not (Test-SafeServerIdentity $server)) {
        throw "The .ikev server address is invalid."
    }
    if (-not (Test-SafeServerIdentity $remoteId)) {
        throw "The .ikev remote identity is invalid."
    }
    if ($remoteId -cne $server) {
        throw "This .ikev profile uses a Remote ID different from the server address.`nWindows client v$APP_VERSION cannot safely represent that profile."
    }

    $username = Get-RequiredIkevString -Object $profile -Name "username"
    if ($username.Length -gt 64 -or $username -notmatch '^[A-Za-z0-9._@+\-]+$') {
        throw "The .ikev username is invalid."
    }

    $authentication = Get-RequiredIkevString -Object $profile -Name "authentication"
    if ($authentication -cne "eap-mschapv2") {
        throw "Unsupported authentication method: $authentication."
    }

    $connection = Get-RequiredIkevProperty -Object $profile -Name "connection"
    $mode = Get-RequiredIkevString -Object $connection -Name "mode"
    if ($mode -cne "full-tunnel") {
        throw "Unsupported connection mode: $mode."
    }

    $serverProfile = Get-RequiredIkevString -Object $profile -Name "server_profile"
    if ($serverProfile -cne "secure" -and $serverProfile -cne "stock-windows-compatible") {
        throw "Unsupported server profile: $serverProfile."
    }

    $ca = Get-RequiredIkevProperty -Object $profile -Name "ca_certificate"
    $caEncoding = Get-RequiredIkevString -Object $ca -Name "encoding"
    if ($caEncoding -cne "der-base64") {
        throw "Unsupported CA certificate encoding: $caEncoding."
    }
    $caData = Get-RequiredIkevString -Object $ca -Name "data"
    $caSha256 = Get-RequiredIkevString -Object $ca -Name "sha256"
    if ($caSha256 -notmatch '^(?:[0-9A-F]{2}:){31}[0-9A-F]{2}$') {
        throw "The embedded CA certificate fingerprint is invalid."
    }

    $proxy = Get-RequiredIkevProperty -Object $profile -Name "proxy"
    $proxyEnabled = Get-RequiredIkevProperty -Object $proxy -Name "enabled"
    if ($proxyEnabled -isnot [bool]) {
        throw "The .ikev profile contains invalid proxy metadata."
    }

    $proxyType = $null
    $proxyHost = $null
    $proxyPort = $null
    if ($proxyEnabled) {
        $proxyType = Get-RequiredIkevString -Object $proxy -Name "type"
        $proxyHost = Get-RequiredIkevString -Object $proxy -Name "host"
        $proxyPortValue = Get-RequiredIkevProperty -Object $proxy -Name "port"

        if ($proxyType -cne "socks5") {
            throw "Unsupported proxy type: $proxyType."
        }
        if (-not (Test-IPv4Address $proxyHost)) {
            throw "The .ikev profile contains invalid proxy metadata."
        }
        if (($proxyPortValue -isnot [int]) -and ($proxyPortValue -isnot [long])) {
            throw "The .ikev profile contains invalid proxy metadata."
        }
        if ($proxyPortValue -lt 1 -or $proxyPortValue -gt 65535) {
            throw "The .ikev profile contains invalid proxy metadata."
        }
        $proxyPort = [int]$proxyPortValue
    }

    return [PSCustomObject]@{
        Name          = $name
        Server        = $server
        RemoteId      = $remoteId
        Username      = $username
        Authentication = $authentication
        ConnectionMode = $mode
        ServerProfile = $serverProfile
        CaData        = $caData
        CaSha256      = $caSha256.ToUpperInvariant()
        ProxyEnabled  = [bool]$proxyEnabled
        ProxyType     = $proxyType
        ProxyHost     = $proxyHost
        ProxyPort     = $proxyPort
    }
}

function Get-IkevCertificateInfo {
    param($Profile)

    try {
        $derBytes = [Convert]::FromBase64String($Profile.CaData)
    }
    catch {
        throw "The embedded CA certificate is invalid."
    }

    if (-not $derBytes -or $derBytes.Length -eq 0) {
        throw "The embedded CA certificate is invalid."
    }

    try {
        $certificate = New-Object `
            System.Security.Cryptography.X509Certificates.X509Certificate2 `
            -ArgumentList (, $derBytes)
    }
    catch {
        throw "The embedded CA certificate is invalid."
    }

    if ($certificate.HasPrivateKey -or $certificate.RawData.Length -ne $derBytes.Length) {
        $certificate.Reset()
        throw "The embedded CA certificate is invalid."
    }
    for ($i = 0; $i -lt $derBytes.Length; $i++) {
        if ($certificate.RawData[$i] -ne $derBytes[$i]) {
            $certificate.Reset()
            throw "The embedded CA certificate is invalid."
        }
    }

    $basicConstraints = $null
    foreach ($extension in $certificate.Extensions) {
        if ($extension.Oid.Value -eq "2.5.29.19") {
            try {
                $basicConstraints = New-Object `
                    System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension
                $basicConstraints.CopyFrom($extension)
            }
            catch {
                $certificate.Reset()
                throw "The embedded CA certificate is invalid."
            }
            break
        }
    }

    if (-not $basicConstraints -or -not $basicConstraints.CertificateAuthority) {
        $certificate.Reset()
        throw "The embedded certificate is not a CA certificate."
    }

    $now = Get-Date
    if ($now -lt $certificate.NotBefore -or $now -gt $certificate.NotAfter) {
        $certificate.Reset()
        throw "The embedded CA certificate is expired or not yet valid."
    }

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash($derBytes)
    }
    finally {
        $sha256.Dispose()
    }
    $calculatedFingerprint = ($hashBytes | ForEach-Object { $_.ToString("X2") }) -join ":"

    if ($calculatedFingerprint -ine $Profile.CaSha256) {
        $certificate.Reset()
        throw "CA certificate fingerprint verification failed.`nThe profile may be corrupted or modified."
    }

    return [PSCustomObject]@{
        Certificate = $certificate
        DerBytes     = $derBytes
        Fingerprint  = $calculatedFingerprint
    }
}

function Get-CertificateDisplayName {
    param($Certificate)

    $name = $Certificate.GetNameInfo(
        [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
        $false
    )
    if ([string]::IsNullOrWhiteSpace($name)) {
        return $Certificate.Subject
    }
    return $name
}

function Ensure-ImportedCaTrusted {
    param(
        $Certificate,
        [byte[]]$DerBytes
    )

    # Thumbprint is used only to locate the same certificate in the Windows store.
    # Portable-profile integrity was already verified with SHA-256 over the DER bytes.
    $trustedCa = Get-ChildItem Cert:\LocalMachine\Root |
        Where-Object { $_.Thumbprint -eq $Certificate.Thumbprint }

    if ($trustedCa) {
        Write-Info "The embedded VPN CA is already trusted."
    }
    else {
        $temporaryCertificate = $null
        try {
            $temporaryCertificate = [System.IO.Path]::GetTempFileName()
            [System.IO.File]::WriteAllBytes($temporaryCertificate, $DerBytes)

            Write-Step "Importing the embedded VPN CA into LocalMachine\Root..."
            Import-Certificate `
                -FilePath $temporaryCertificate `
                -CertStoreLocation "Cert:\LocalMachine\Root" | Out-Null

            $trustedCa = Get-ChildItem Cert:\LocalMachine\Root |
                Where-Object { $_.Thumbprint -eq $Certificate.Thumbprint }
            if (-not $trustedCa) {
                throw "The CA import command completed, but the certificate was not found in LocalMachine\Root."
            }
        }
        catch {
            throw "Failed to import the embedded VPN CA into LocalMachine\Root: $($_.Exception.Message)"
        }
        finally {
            if ($temporaryCertificate) {
                Remove-Item -LiteralPath $temporaryCertificate -Force -ErrorAction SilentlyContinue
            }
        }

        Write-Host "Embedded VPN CA imported successfully." -ForegroundColor Green
    }

    return [PSCustomObject]@{
        File       = $null
        Name       = (Get-CertificateDisplayName -Certificate $Certificate)
        Subject    = $Certificate.Subject
        Thumbprint = $Certificate.Thumbprint
    }
}

function Prompt-ImportedTrafficMode {
    param($Profile)

    if (-not $Profile.ProxyEnabled) {
        return [PSCustomObject]@{
            Mode      = "FullTunnel"
            ProxyHost = $null
            ProxyPort = $null
        }
    }

    Write-Host ""
    Write-Host "Traffic Mode" -ForegroundColor Green
    Write-Host "============"
    Write-Host ""
    Write-Host "1) Full Tunnel"
    Write-Host "   Route all IPv4 traffic through the VPN."
    Write-Host ""
    Write-Host "2) Proxy Mode"
    Write-Host "   Route only $($Profile.ProxyHost)/32 through the VPN."
    Write-Host "   SOCKS5 endpoint: $($Profile.ProxyHost):$($Profile.ProxyPort)"
    Write-Host "   All other Windows traffic stays DIRECT."
    Write-Host ""

    while ($true) {
        $choice = (Read-Host "Choose [1-2] (default: 1)").Trim()
        if ([string]::IsNullOrWhiteSpace($choice) -or $choice -eq "1") {
            return [PSCustomObject]@{
                Mode      = "FullTunnel"
                ProxyHost = $null
                ProxyPort = $null
            }
        }
        if ($choice -eq "2") {
            return [PSCustomObject]@{
                Mode      = "ProxyMode"
                ProxyHost = $Profile.ProxyHost
                ProxyPort = [int]$Profile.ProxyPort
            }
        }
        Write-Warn "Invalid selection."
    }
}

function Get-ProfileStatePath {
    param(
        [string]$Name,
        [bool]$AllUser
    )

    $scope = if ($AllUser) { "all" } else { "user" }
    $material = "$scope|$Name"
    $sha = [System.Security.Cryptography.SHA256]::Create()

    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($material)
        $hashBytes = $sha.ComputeHash($bytes)
        $hash = -join ($hashBytes | ForEach-Object { $_.ToString("x2") })
    }
    finally {
        $sha.Dispose()
    }

    return Join-Path $PROFILE_STATE_DIR ("{0}.json" -f $hash.Substring(0, 24))
}

function Save-ProxySettings {
    param(
        [string]$Name,
        [bool]$AllUser,
        [string]$ProxyHost,
        [int]$ProxyPort
    )

    if (-not (Test-Path -LiteralPath $PROFILE_STATE_DIR)) {
        New-Item -ItemType Directory -Path $PROFILE_STATE_DIR -Force | Out-Null
    }

    $state = [PSCustomObject]@{
        ProfileName = $Name
        AllUser     = $AllUser
        ProxyHost   = $ProxyHost
        ProxyPort   = $ProxyPort
    }

    $path = Get-ProfileStatePath -Name $Name -AllUser $AllUser
    $state | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding UTF8
}

function Get-ProxySettings {
    param(
        [string]$Name,
        [bool]$AllUser
    )

    $path = Get-ProfileStatePath -Name $Name -AllUser $AllUser

    if (Test-Path -LiteralPath $path) {
        try {
            $state = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            if ((Test-IPv4Address ([string]$state.ProxyHost)) -and (Test-TcpPort ([string]$state.ProxyPort))) {
                return [PSCustomObject]@{
                    ProxyHost = [string]$state.ProxyHost
                    ProxyPort = [int]$state.ProxyPort
                    IsSaved   = $true
                }
            }
        }
        catch {
            Write-Warn "Saved Proxy Mode settings for '$Name' could not be read. Defaults will be used."
        }
    }

    return [PSCustomObject]@{
        ProxyHost = $DEFAULT_PROXY_HOST
        ProxyPort = $DEFAULT_PROXY_PORT
        IsSaved   = $false
    }
}

function Remove-ProxyState {
    param(
        [string]$Name,
        [bool]$AllUser
    )

    $path = Get-ProfileStatePath -Name $Name -AllUser $AllUser
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}

function Get-VpnObjectForProfile {
    param($Profile)

    if ($Profile.AllUser) {
        return Get-VpnConnection -Name $Profile.Name -AllUserConnection -ErrorAction SilentlyContinue
    }

    return Get-VpnConnection -Name $Profile.Name -ErrorAction SilentlyContinue
}

function Remove-ManagedProxyRoute {
    param(
        $Profile,
        [string]$ProxyHost
    )

    if (-not $Profile -or -not (Test-IPv4Address $ProxyHost)) {
        return
    }

    $prefix = "$ProxyHost/32"

    try {
        if ($Profile.AllUser) {
            Remove-VpnConnectionRoute `
                -ConnectionName $Profile.Name `
                -DestinationPrefix $prefix `
                -AllUserConnection `
                -ErrorAction SilentlyContinue | Out-Null
        }
        else {
            Remove-VpnConnectionRoute `
                -ConnectionName $Profile.Name `
                -DestinationPrefix $prefix `
                -ErrorAction SilentlyContinue | Out-Null
        }
    }
    catch {
        # Missing routes are harmless. Do not touch routes not created by this utility.
    }
}

function Add-ManagedProxyRoute {
    param(
        $Profile,
        [string]$ProxyHost
    )

    $prefix = "$ProxyHost/32"

    Remove-ManagedProxyRoute -Profile $Profile -ProxyHost $ProxyHost

    if ($Profile.AllUser) {
        Add-VpnConnectionRoute `
            -ConnectionName $Profile.Name `
            -DestinationPrefix $prefix `
            -RouteMetric 1 `
            -AllUserConnection `
            -PassThru `
            -ErrorAction Stop | Out-Null
    }
    else {
        Add-VpnConnectionRoute `
            -ConnectionName $Profile.Name `
            -DestinationPrefix $prefix `
            -RouteMetric 1 `
            -PassThru `
            -ErrorAction Stop | Out-Null
    }
}

function Set-ProfileSplitTunneling {
    param(
        $Profile,
        [bool]$Enabled
    )

    if ($Profile.AllUser) {
        Set-VpnConnection `
            -Name $Profile.Name `
            -SplitTunneling $Enabled `
            -AllUserConnection `
            -Force `
            -ErrorAction Stop | Out-Null
    }
    else {
        Set-VpnConnection `
            -Name $Profile.Name `
            -SplitTunneling $Enabled `
            -Force `
            -ErrorAction Stop | Out-Null
    }
}

function Get-TrafficModeInfo {
    param($Profile)

    $vpn = Get-VpnObjectForProfile -Profile $Profile
    if (-not $vpn) {
        return [PSCustomObject]@{
            Mode = "Unknown"
            ProxySettings = $null
        }
    }

    if (-not $vpn.SplitTunneling) {
        return [PSCustomObject]@{
            Mode = "Full Tunnel"
            ProxySettings = $null
        }
    }

    $proxySettings = Get-ProxySettings -Name $Profile.Name -AllUser $Profile.AllUser

    if ($proxySettings.IsSaved) {
        return [PSCustomObject]@{
            Mode = "Proxy Mode"
            ProxySettings = $proxySettings
        }
    }

    return [PSCustomObject]@{
        Mode = "Split Tunnel (custom/unknown)"
        ProxySettings = $proxySettings
    }
}

function Prompt-TrafficMode {
    Write-Host ""
    Write-Host "Traffic Mode" -ForegroundColor Green
    Write-Host "============"
    Write-Host ""
    Write-Host "1) Full Tunnel"
    Write-Host "   Route all IPv4 traffic through the VPN."
    Write-Host ""
    Write-Host "2) Proxy Mode"
    Write-Host "   Route only the private SOCKS5 endpoint through IKEv2."
    Write-Host "   All other Windows traffic stays DIRECT."
    Write-Host ""

    while ($true) {
        $choice = (Read-Host "Choose traffic mode [1-2] (default: 1)").Trim()
        if ([string]::IsNullOrWhiteSpace($choice)) {
            $choice = "1"
        }

        switch ($choice) {
            "1" {
                return [PSCustomObject]@{
                    Mode = "FullTunnel"
                    ProxyHost = $null
                    ProxyPort = $null
                }
            }
            "2" {
                $proxyHost = (Read-Host "SOCKS5 proxy IP [$DEFAULT_PROXY_HOST]").Trim()
                if ([string]::IsNullOrWhiteSpace($proxyHost)) {
                    $proxyHost = $DEFAULT_PROXY_HOST
                }

                if (-not (Test-IPv4Address $proxyHost)) {
                    Write-Warn "Enter a valid IPv4 proxy address."
                    continue
                }

                $proxyPortText = (Read-Host "SOCKS5 proxy port [$DEFAULT_PROXY_PORT]").Trim()
                if ([string]::IsNullOrWhiteSpace($proxyPortText)) {
                    $proxyPortText = [string]$DEFAULT_PROXY_PORT
                }

                if (-not (Test-TcpPort $proxyPortText)) {
                    Write-Warn "Enter a TCP port between 1 and 65535."
                    continue
                }

                return [PSCustomObject]@{
                    Mode = "ProxyMode"
                    ProxyHost = $proxyHost
                    ProxyPort = [int]$proxyPortText
                }
            }
            default {
                Write-Warn "Invalid selection."
            }
        }
    }
}

function Apply-TrafficMode {
    param(
        $Profile,
        $ModeSelection
    )

    if (-not $Profile -or -not $ModeSelection) {
        return $false
    }

    $oldSettings = Get-ProxySettings -Name $Profile.Name -AllUser $Profile.AllUser

    if ($ModeSelection.Mode -eq "FullTunnel") {
        Write-Step "Enabling Full Tunnel mode..."

        if ($oldSettings.IsSaved) {
            Remove-ManagedProxyRoute -Profile $Profile -ProxyHost $oldSettings.ProxyHost
        }
        else {
            Remove-ManagedProxyRoute -Profile $Profile -ProxyHost $DEFAULT_PROXY_HOST
        }

        Set-ProfileSplitTunneling -Profile $Profile -Enabled $false
        Remove-ProxyState -Name $Profile.Name -AllUser $Profile.AllUser

        Write-Host ""
        Write-Host "Full Tunnel mode enabled." -ForegroundColor Green
        Write-Info "All IPv4 traffic will use the VPN while connected."
        return $true
    }

    if ($ModeSelection.Mode -eq "ProxyMode") {
        if ($oldSettings.IsSaved -and $oldSettings.ProxyHost -ne $ModeSelection.ProxyHost) {
            Remove-ManagedProxyRoute -Profile $Profile -ProxyHost $oldSettings.ProxyHost
        }

        Write-Step "Enabling split tunneling for Proxy Mode..."
        Set-ProfileSplitTunneling -Profile $Profile -Enabled $true

        Write-Step "Routing only $($ModeSelection.ProxyHost)/32 through the VPN..."
        Add-ManagedProxyRoute -Profile $Profile -ProxyHost $ModeSelection.ProxyHost

        Save-ProxySettings `
            -Name $Profile.Name `
            -AllUser $Profile.AllUser `
            -ProxyHost $ModeSelection.ProxyHost `
            -ProxyPort $ModeSelection.ProxyPort

        Write-Host ""
        Write-Host "Proxy Mode enabled." -ForegroundColor Green
        Write-Host "  SOCKS5 host : $($ModeSelection.ProxyHost)"
        Write-Host "  SOCKS5 port : $($ModeSelection.ProxyPort)"
        Write-Host "  VPN route   : $($ModeSelection.ProxyHost)/32 only"
        Write-Host "  Other traffic: DIRECT"
        Write-Host ""
        Write-Info "Configure only the applications that should use the VPN to use this SOCKS5 endpoint."
        Write-Info "For SOCKS-capable applications, enable remote/proxy DNS when available to avoid DNS leaks."
        return $true
    }

    throw "Unknown traffic mode: $($ModeSelection.Mode)"
}

function Configure-TrafficMode {
    $profile = Select-Ikev2Profile -Action "configure traffic mode for"
    if (-not $profile) {
        return
    }

    $current = Get-VpnObjectForProfile -Profile $profile
    if (-not $current) {
        Write-Warn "The selected VPN profile no longer exists."
        return
    }

    if ($current.ConnectionStatus -eq "Connected") {
        Write-Warn "Changing routing while the VPN is connected can leave the current session with stale routes."
        Write-Info "The new mode will be stored now. Disconnect and reconnect the VPN after this change."
    }

    $currentMode = Get-TrafficModeInfo -Profile $profile
    Write-Host ""
    Write-Host "Current traffic mode: $($currentMode.Mode)"
    if ($currentMode.ProxySettings -and $currentMode.ProxySettings.IsSaved) {
        Write-Host "Current SOCKS5 endpoint: $($currentMode.ProxySettings.ProxyHost):$($currentMode.ProxySettings.ProxyPort)"
    }

    $selection = Prompt-TrafficMode

    try {
        Apply-TrafficMode -Profile $profile -ModeSelection $selection | Out-Null
    }
    catch {
        Write-Host "[!] Failed to change traffic mode: $($_.Exception.Message)" -ForegroundColor Red
        return
    }

    if ($current.ConnectionStatus -eq "Connected") {
        Write-Warn "Disconnect and reconnect '$($profile.Name)' before relying on the new routing mode."
    }
}

function Test-ProxyEndpointForProfile {
    param($Profile)

    if (-not $Profile) {
        return
    }

    $mode = Get-TrafficModeInfo -Profile $Profile
    if ($mode.Mode -ne "Proxy Mode" -or -not $mode.ProxySettings) {
        return
    }

    $hostName = $mode.ProxySettings.ProxyHost
    $port = $mode.ProxySettings.ProxyPort

    Write-Step "Checking private SOCKS5 endpoint $hostName`:$port..."

    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($hostName, $port, $null, $null)
        $connected = $async.AsyncWaitHandle.WaitOne(3000, $false)

        if ($connected -and $client.Connected) {
            $client.EndConnect($async)
            Write-Host "SOCKS5 endpoint is reachable through the VPN." -ForegroundColor Green
        }
        else {
            Write-Warn "SOCKS5 endpoint did not answer within 3 seconds."
            Write-Info "Check that server v6 Proxy Mode is enabled and that the proxy IP/port match."
        }
    }
    catch {
        Write-Warn "SOCKS5 endpoint connectivity check failed: $($_.Exception.Message)"
    }
    finally {
        if ($client) {
            $client.Close()
        }
    }
}

function Get-AllIkev2Profiles {
    $profiles = @()

    try {
        $currentUserProfiles = @(Get-VpnConnection -ErrorAction SilentlyContinue)
        foreach ($vpn in $currentUserProfiles) {
            if ($vpn.TunnelType.ToString() -ieq "Ikev2") {
                $profiles += [PSCustomObject]@{
                    Name             = $vpn.Name
                    ServerAddress    = $vpn.ServerAddress
                    ConnectionStatus = $vpn.ConnectionStatus
                    TunnelType       = $vpn.TunnelType
                    Scope            = "Current User"
                    AllUser          = $false
                    VpnObject        = $vpn
                }
            }
        }
    }
    catch {
    }

    try {
        $allUserProfiles = @(Get-VpnConnection -AllUserConnection -ErrorAction SilentlyContinue)
        foreach ($vpn in $allUserProfiles) {
            if ($vpn.TunnelType.ToString() -ieq "Ikev2") {
                $profiles += [PSCustomObject]@{
                    Name             = $vpn.Name
                    ServerAddress    = $vpn.ServerAddress
                    ConnectionStatus = $vpn.ConnectionStatus
                    TunnelType       = $vpn.TunnelType
                    Scope            = "All Users"
                    AllUser          = $true
                    VpnObject        = $vpn
                }
            }
        }
    }
    catch {
    }

    return @(
        $profiles |
        Sort-Object Scope, Name
    )
}

function Select-Ikev2Profile {
    param(
        [string]$Action = "select"
    )

    $profiles = @(Get-AllIkev2Profiles)

    if ($profiles.Count -eq 0) {
        Write-Warn "No IKEv2 VPN profiles were found in Windows."
        return $null
    }

    Write-Host ""
    Write-Host "Available IKEv2 VPN profiles" -ForegroundColor Green
    Write-Host "============================"
    Write-Host ""

    for ($i = 0; $i -lt $profiles.Count; $i++) {
        $vpn = $profiles[$i]
        $number = $i + 1
        Write-Host ("{0}) {1}" -f $number, $vpn.Name)
        Write-Host ("   Server : {0}" -f $vpn.ServerAddress)
        Write-Host ("   Scope  : {0}" -f $vpn.Scope)
        Write-Host ("   Status : {0}" -f $vpn.ConnectionStatus)
        Write-Host ""
    }

    Write-Host "0) Cancel"
    Write-Host ""

    while ($true) {
        $choice = (Read-Host "Choose a VPN profile to $Action [0-$($profiles.Count)]").Trim()

        if ($choice -eq "0") {
            return $null
        }

        $index = 0
        if ([int]::TryParse($choice, [ref]$index)) {
            if ($index -ge 1 -and $index -le $profiles.Count) {
                return $profiles[$index - 1]
            }
        }

        Write-Warn "Invalid selection."
    }
}

function Get-PhonebookPath {
    param([bool]$AllUser)

    if ($AllUser) {
        return Join-Path $env:ProgramData "Microsoft\Network\Connections\Pbk\rasphone.pbk"
    }

    return Join-Path $env:AppData "Microsoft\Network\Connections\Pbk\rasphone.pbk"
}

function Show-VpnProfileStatus {
    param($Profile)

    if (-not $Profile) {
        return
    }

    try {
        if ($Profile.AllUser) {
            $vpn = Get-VpnConnection `
                -Name $Profile.Name `
                -AllUserConnection `
                -ErrorAction Stop
        }
        else {
            $vpn = Get-VpnConnection `
                -Name $Profile.Name `
                -ErrorAction Stop
        }
    }
    catch {
        Write-Warn "The selected VPN profile could not be read."
        return
    }

    Write-Host ""
    Write-Host "VPN Status" -ForegroundColor Green
    Write-Host "=========="
    Write-Host ""
    Write-Host "Profile          : $($vpn.Name)"
    Write-Host "Server           : $($vpn.ServerAddress)"
    Write-Host "Scope            : $($Profile.Scope)"
    Write-Host "Tunnel type      : $($vpn.TunnelType)"
    Write-Host "Connection       : $($vpn.ConnectionStatus)"
    Write-Host "Authentication   : $($vpn.AuthenticationMethod -join ', ')"
    Write-Host "Remember creds   : $($vpn.RememberCredential)"
    Write-Host "Split tunneling  : $($vpn.SplitTunneling)"

    $modeInfo = Get-TrafficModeInfo -Profile $Profile
    Write-Host "Traffic mode     : $($modeInfo.Mode)"
    if ($modeInfo.Mode -eq "Proxy Mode" -and $modeInfo.ProxySettings) {
        Write-Host "SOCKS5 proxy     : $($modeInfo.ProxySettings.ProxyHost):$($modeInfo.ProxySettings.ProxyPort)"
        Write-Host "VPN-only route   : $($modeInfo.ProxySettings.ProxyHost)/32"
    }

    Write-Host ""
    Write-Host "IPsec Policy"
    Write-Host "  Windows does not provide a Get-VpnConnectionIPsecConfiguration cmdlet."
    Write-Host "  Profiles created by this utility are configured with:"
    Write-Host "    IKE encryption : AES256"
    Write-Host "    IKE integrity  : SHA256"
    Write-Host "    DH group       : Group14"
    Write-Host "    ESP cipher     : AES256"
    Write-Host "    ESP auth       : SHA256128"
    Write-Host "    PFS            : None"
    Write-Host ""
}

function Install-Ikev2ProfileCore {
    param(
        [string]$ConnectionName,
        [string]$ServerAddress,
        $TrafficMode,
        [bool]$ReplaceExisting
    )

    Write-Step "Checking for an existing all-users profile..."
    $existingVpn = Get-VpnConnection `
        -Name $ConnectionName `
        -AllUserConnection `
        -ErrorAction SilentlyContinue

    if ($existingVpn -and -not $ReplaceExisting) {
        Write-Warn "Profile '$ConnectionName' now exists and was not replaced."
        return $null
    }

    if ($existingVpn) {
        $existingProfile = [PSCustomObject]@{
            Name          = $ConnectionName
            AllUser       = $true
            ServerAddress = $existingVpn.ServerAddress
        }
        $oldProxySettings = Get-ProxySettings -Name $ConnectionName -AllUser $true

        if ($existingVpn.ConnectionStatus -eq "Connected") {
            Write-Step "Disconnecting the existing VPN connection..."
            $phonebook = Get-PhonebookPath -AllUser $true
            & "$env:SystemRoot\System32\rasdial.exe" `
                $ConnectionName `
                /disconnect `
                "/phonebook:$phonebook" | Out-Null
            Start-Sleep -Seconds 1
        }

        if ($oldProxySettings.IsSaved) {
            Remove-ManagedProxyRoute `
                -Profile $existingProfile `
                -ProxyHost $oldProxySettings.ProxyHost
        }
        Remove-ProxyState -Name $ConnectionName -AllUser $true

        Write-Step "Replacing the existing VPN profile..."
        Remove-VpnConnection `
            -Name $ConnectionName `
            -AllUserConnection `
            -Force
    }

    Write-Step "Creating the IKEv2 VPN profile..."

    try {
        $eap = New-EapConfiguration

        Add-VpnConnection `
            -Name $ConnectionName `
            -ServerAddress $ServerAddress `
            -TunnelType Ikev2 `
            -AuthenticationMethod Eap `
            -EapConfigXmlStream $eap.EapConfigXmlStream `
            -EncryptionLevel Required `
            -RememberCredential `
            -SplitTunneling:($TrafficMode.Mode -eq "ProxyMode") `
            -AllUserConnection `
            -Force | Out-Null
    }
    catch {
        Write-Host "[!] Failed to create the VPN profile: $($_.Exception.Message)" -ForegroundColor Red
        return $null
    }

    Write-Step "Applying the IPsec cryptographic policy..."

    try {
        $appliedIpsec = Set-VpnConnectionIPsecConfiguration `
            -ConnectionName $ConnectionName `
            -AuthenticationTransformConstants SHA256128 `
            -CipherTransformConstants AES256 `
            -DHGroup Group14 `
            -EncryptionMethod AES256 `
            -IntegrityCheckMethod SHA256 `
            -PfsGroup None `
            -AllUserConnection `
            -PassThru `
            -Force
    }
    catch {
        Write-Host "[!] Failed to apply the IPsec policy: $($_.Exception.Message)" -ForegroundColor Red
        return $null
    }

    if ($appliedIpsec) {
        Write-Info "Windows accepted the custom IPsec policy."
        Write-Host "  IKE encryption : $($appliedIpsec.EncryptionMethod)"
        Write-Host "  IKE integrity  : $($appliedIpsec.IntegrityCheckMethod)"
        Write-Host "  DH group       : $($appliedIpsec.DHGroup)"
        Write-Host "  ESP cipher     : $($appliedIpsec.CipherTransformConstants)"
        Write-Host "  ESP auth       : $($appliedIpsec.AuthenticationTransformConstants)"
        Write-Host "  PFS            : $($appliedIpsec.PfsGroup)"
    }

    $newProfile = [PSCustomObject]@{
        Name             = $ConnectionName
        ServerAddress    = $ServerAddress
        ConnectionStatus = "Disconnected"
        TunnelType       = "Ikev2"
        Scope            = "All Users"
        AllUser          = $true
    }

    try {
        Apply-TrafficMode -Profile $newProfile -ModeSelection $TrafficMode | Out-Null
    }
    catch {
        Write-Host "[!] VPN profile was created, but Traffic Mode configuration failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Warn "The profile has been left installed for troubleshooting."
        return $null
    }

    return $newProfile
}

function Install-OrUpdateVpn {
    Write-Host ""
    Write-Host "Install / Update IKEv2 VPN" -ForegroundColor Green
    Write-Host "==========================="
    Write-Host ""

    try {
        $manualCaFile = Get-CaCertificateFile -AllowMissing
        if (-not $manualCaFile) {
            Write-Warn "Manual setup requires ca-cert.cer next to this script."
            Write-Info "Alternatively, import a portable .ikev profile."
            return
        }

        $script:caInfo = Ensure-CaTrusted -CertificateFile $manualCaFile
    }
    catch {
        Write-Host "[!] $($_.Exception.Message)" -ForegroundColor Red
        return
    }

    $defaultName = "IKEv2 VPN"
    $enteredName = (Read-Host "VPN profile name [$defaultName]").Trim()

    if ([string]::IsNullOrWhiteSpace($enteredName)) {
        $ConnectionName = $defaultName
    }
    else {
        $ConnectionName = $enteredName
    }

    do {
        $ServerAddress = (Read-Host "VPN server IP or hostname").Trim()
    } while ([string]::IsNullOrWhiteSpace($ServerAddress))

    $trafficMode = Prompt-TrafficMode

    Write-Host ""
    Write-Info "Windows will request the VPN username and password in its native connection dialog."
    Write-Host ""

    $newProfile = Install-Ikev2ProfileCore `
        -ConnectionName $ConnectionName `
        -ServerAddress $ServerAddress `
        -TrafficMode $trafficMode `
        -ReplaceExisting $true
    if (-not $newProfile) {
        return
    }

    Write-Host ""
    $connectNow = (Read-Host "VPN profile created. Connect now? [Y/n]").Trim()

    if ($connectNow -notmatch '^(?i)n(o)?$') {
        Connect-VpnProfile -Profile $newProfile
    }
    else {
        Write-Host "VPN profile created successfully." -ForegroundColor Green
    }
}

function Import-IkevProfile {
    Write-Host ""
    Write-Host "Import .ikev VPN Profile" -ForegroundColor Green
    Write-Host "========================"
    Write-Host ""

    $profilePath = (Read-Host "Path to .ikev profile").Trim()
    if ([string]::IsNullOrWhiteSpace($profilePath)) {
        Write-Warn "The .ikev profile path must be a readable regular file."
        return
    }

    try {
        $importedProfile = Read-IkevProfile -Path $profilePath
        $certificateInfo = Get-IkevCertificateInfo -Profile $importedProfile
    }
    catch {
        $_.Exception.Message -split "`r?`n" | ForEach-Object { Write-Warn $_ }
        return
    }

    try {
        $proxySummary = if ($importedProfile.ProxyEnabled) {
            "available ($($importedProfile.ProxyHost):$($importedProfile.ProxyPort))"
        }
        else {
            "not advertised"
        }

        Write-Host ""
        Write-Host "IKEv Profile" -ForegroundColor Green
        Write-Host "============"
        Write-Host ""
        Write-Host "Name        : $($importedProfile.Name)"
        Write-Host "Server      : $($importedProfile.Server)"
        Write-Host "Remote ID   : $($importedProfile.RemoteId)"
        Write-Host "Username    : $($importedProfile.Username)"
        Write-Host "Auth        : EAP-MSCHAPv2"
        Write-Host "Mode        : Full tunnel"
        Write-Host "CA SHA-256  : $($certificateInfo.Fingerprint)"
        Write-Host "Server type : $($importedProfile.ServerProfile)"
        Write-Host "Proxy Mode  : $proxySummary"
        Write-Host ""

        $trust = (Read-Host "Import and trust this VPN profile? [y/N]").Trim()
        if ($trust -notmatch '^(?i)y(es)?$') {
            Write-Info "Import canceled."
            return
        }

        $trafficMode = Prompt-ImportedTrafficMode -Profile $importedProfile

        $existingVpn = Get-VpnConnection `
            -Name $importedProfile.Name `
            -AllUserConnection `
            -ErrorAction SilentlyContinue
        $replaceExisting = $false
        if ($existingVpn) {
            Write-Host "Profile '$($importedProfile.Name)' already exists."
            $replace = (Read-Host "Replace it? [y/N]").Trim()
            if ($replace -notmatch '^(?i)y(es)?$') {
                Write-Info "Import canceled."
                return
            }
            $replaceExisting = $true
        }

        try {
            $script:caInfo = Ensure-ImportedCaTrusted `
                -Certificate $certificateInfo.Certificate `
                -DerBytes $certificateInfo.DerBytes
        }
        catch {
            Write-Host "[!] $($_.Exception.Message)" -ForegroundColor Red
            return
        }

        $newProfile = Install-Ikev2ProfileCore `
            -ConnectionName $importedProfile.Name `
            -ServerAddress $importedProfile.Server `
            -TrafficMode $trafficMode `
            -ReplaceExisting $replaceExisting
        if (-not $newProfile) {
            return
        }

        $modeName = if ($trafficMode.Mode -eq "ProxyMode") { "Proxy Mode" } else { "Full Tunnel" }

        Write-Host ""
        Write-Host "VPN profile imported successfully." -ForegroundColor Green
        Write-Host ""
        Write-Host "Profile : $($importedProfile.Name)"
        Write-Host "Server  : $($importedProfile.Server)"
        Write-Host "User    : $($importedProfile.Username)"
        Write-Host "Mode    : $modeName"
        if ($trafficMode.Mode -eq "ProxyMode") {
            Write-Host "SOCKS5  : $($trafficMode.ProxyHost):$($trafficMode.ProxyPort)"
        }
        Write-Host ""

        $connectNow = (Read-Host "Connect now? [Y/n]").Trim()
        if ($connectNow -notmatch '^(?i)n(o)?$') {
            Write-Info "Use VPN username: $($importedProfile.Username)"
            Write-Info "Windows will request the VPN password in its native EAP dialog."
            Connect-VpnProfile -Profile $newProfile
        }
    }
    finally {
        if ($certificateInfo -and $certificateInfo.Certificate) {
            $certificateInfo.Certificate.Reset()
        }
    }
}

function Connect-VpnProfile {
    param($Profile)

    if (-not $Profile) {
        return
    }

    if ($Profile.AllUser) {
        $current = Get-VpnConnection `
            -Name $Profile.Name `
            -AllUserConnection `
            -ErrorAction SilentlyContinue
    }
    else {
        $current = Get-VpnConnection `
            -Name $Profile.Name `
            -ErrorAction SilentlyContinue
    }

    if (-not $current) {
        Write-Warn "The selected VPN profile no longer exists."
        return
    }

    if ($current.ConnectionStatus -eq "Connected") {
        Write-Info "VPN profile '$($Profile.Name)' is already connected."
        Show-VpnProfileStatus -Profile $Profile
        Test-ProxyEndpointForProfile -Profile $Profile
        return
    }

    $phonebook = Get-PhonebookPath -AllUser $Profile.AllUser

    Write-Host ""
    Write-Host "Connect IKEv2 VPN" -ForegroundColor Green
    Write-Host "=================="
    Write-Host ""
    Write-Host "Profile: $($Profile.Name)"
    Write-Host "Server : $($Profile.ServerAddress)"
    Write-Host ""

    Write-Step "Trying saved Windows VPN credentials..."

    $rasOutput = & "$env:SystemRoot\System32\rasdial.exe" `
        $Profile.Name `
        "/phonebook:$phonebook" 2>&1

    $rasExitCode = $LASTEXITCODE

    if ($rasExitCode -eq 0) {
        Write-Host ""
        Write-Host "VPN connected successfully using saved credentials." -ForegroundColor Green
        Start-Sleep -Seconds 1
        Show-VpnProfileStatus -Profile $Profile
        Test-ProxyEndpointForProfile -Profile $Profile
        return
    }

    Write-Info "Saved credentials are unavailable or Windows requires interactive EAP authentication."
    Write-Step "Opening the native Windows VPN credential dialog..."

    try {
        $arguments = @(
            "-f", "`"$phonebook`"",
            "-d", "`"$($Profile.Name)`""
        ) -join " "

        $process = Start-Process `
            -FilePath "$env:SystemRoot\System32\rasphone.exe" `
            -ArgumentList $arguments `
            -PassThru

        $process.WaitForExit()
    }
    catch {
        Write-Host ""
        Write-Host "[!] Failed to open the Windows VPN dialog: $($_.Exception.Message)" -ForegroundColor Red
        return
    }

    Start-Sleep -Seconds 1

    if ($Profile.AllUser) {
        $current = Get-VpnConnection `
            -Name $Profile.Name `
            -AllUserConnection `
            -ErrorAction SilentlyContinue
    }
    else {
        $current = Get-VpnConnection `
            -Name $Profile.Name `
            -ErrorAction SilentlyContinue
    }

    Write-Host ""

    if ($current -and $current.ConnectionStatus -eq "Connected") {
        Write-Host "VPN connected successfully." -ForegroundColor Green
        Write-Info "Windows can reuse the saved credentials on future connections."
    }
    else {
        Write-Warn "The VPN is still disconnected."
        Write-Host "If the Windows dialog showed an error, check the server address, certificate trust, username, password, and server logs."
    }

    Show-VpnProfileStatus -Profile $Profile

    if ($current -and $current.ConnectionStatus -eq "Connected") {
        Test-ProxyEndpointForProfile -Profile $Profile
    }
}

function Disconnect-VpnProfile {
    param($Profile)

    if (-not $Profile) {
        return
    }

    if ($Profile.AllUser) {
        $current = Get-VpnConnection `
            -Name $Profile.Name `
            -AllUserConnection `
            -ErrorAction SilentlyContinue
    }
    else {
        $current = Get-VpnConnection `
            -Name $Profile.Name `
            -ErrorAction SilentlyContinue
    }

    if (-not $current) {
        Write-Warn "The selected VPN profile no longer exists."
        return
    }

    if ($current.ConnectionStatus -ne "Connected") {
        Write-Info "VPN profile '$($Profile.Name)' is already disconnected."
        Show-VpnProfileStatus -Profile $Profile
        return
    }

    $phonebook = Get-PhonebookPath -AllUser $Profile.AllUser

    Write-Host ""
    Write-Step "Disconnecting '$($Profile.Name)'..."

    try {
        $arguments = @(
            "-f", "`"$phonebook`"",
            "-h", "`"$($Profile.Name)`""
        ) -join " "

        $process = Start-Process `
            -FilePath "$env:SystemRoot\System32\rasphone.exe" `
            -ArgumentList $arguments `
            -PassThru `
            -Wait

        $rasExitCode = $process.ExitCode
        $rasOutput = @()
    }
    catch {
        $rasOutput = & "$env:SystemRoot\System32\rasdial.exe" `
            $Profile.Name `
            /disconnect `
            "/phonebook:$phonebook" 2>&1
        $rasExitCode = $LASTEXITCODE
    }

    Start-Sleep -Seconds 1

    if ($Profile.AllUser) {
        $current = Get-VpnConnection `
            -Name $Profile.Name `
            -AllUserConnection `
            -ErrorAction SilentlyContinue
    }
    else {
        $current = Get-VpnConnection `
            -Name $Profile.Name `
            -ErrorAction SilentlyContinue
    }

    if ($rasExitCode -eq 0 -and $current.ConnectionStatus -ne "Connected") {
        Write-Host ""
        Write-Host "VPN disconnected successfully." -ForegroundColor Green
    }
    else {
        Write-Host ""
        Write-Warn "Windows did not confirm a clean disconnect."
        $rasOutput | ForEach-Object { Write-Host $_ }

        Write-Host ""
        $force = (Read-Host "Force-disconnect all Windows RAS/VPN sessions by restarting RasMan? [y/N]").Trim()

        if ($force -match '^(?i)y(es)?$') {
            Write-Warn "This will disconnect every Windows RAS/VPN session on this computer."

            try {
                Restart-Service -Name RasMan -Force -ErrorAction Stop
                Start-Sleep -Seconds 2
                Write-Host "RasMan restarted." -ForegroundColor Green
            }
            catch {
                Write-Host "[!] Failed to restart RasMan: $($_.Exception.Message)" -ForegroundColor Red
            }
        }
    }

    Show-VpnProfileStatus -Profile $Profile
}

function Check-RequiredCommands {
    $requiredCommands = @(
        "Add-VpnConnection",
        "Remove-VpnConnection",
        "Set-VpnConnection",
        "Add-VpnConnectionRoute",
        "Remove-VpnConnectionRoute",
        "Set-VpnConnectionIPsecConfiguration",
        "Get-VpnConnection",
        "New-EapConfiguration",
        "Import-Certificate"
    )

    foreach ($command in $requiredCommands) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "Required Windows command is unavailable: $command"
        }
    }
}

Ensure-Administrator

try {
    Check-RequiredCommands
}
catch {
    Write-Host "[!] $($_.Exception.Message)" -ForegroundColor Red
    Pause-Menu
    exit 1
}

$caInfo = [PSCustomObject]@{
    File       = $null
    Name       = "no local CA loaded"
    Subject    = $null
    Thumbprint = $null
}

try {
    $localCaFile = Get-CaCertificateFile -AllowMissing
    if ($localCaFile) {
        $caInfo = Ensure-CaTrusted -CertificateFile $localCaFile
    }
}
catch {
    Write-Host ""
    Write-Host "[!] $($_.Exception.Message)" -ForegroundColor Red
    Pause-Menu
    exit 1
}

Start-Sleep -Seconds 1

while ($true) {
    Clear-Host
    $menuTitle = "IKEv2 Windows VPN Utility v$APP_VERSION"
    Write-Host $menuTitle -ForegroundColor Green
    Write-Host ("=" * $menuTitle.Length) -ForegroundColor Green
    Write-Host ""
    Write-Host "Trusted CA : $($caInfo.Name)"
    Write-Host "Credentials: Windows native EAP dialog"
    Write-Host ""
    Write-Host "1) Install / Update IKEv2 VPN"
    Write-Host "2) Import .ikev Profile"
    Write-Host "3) Status"
    Write-Host "4) Connect"
    Write-Host "5) Disconnect"
    Write-Host "6) Traffic Mode (Full Tunnel / Proxy Mode)"
    Write-Host "7) Exit"
    Write-Host ""

    $choice = (Read-Host "Choose an option [1-7]").Trim()

    switch ($choice) {
        "1" {
            Install-OrUpdateVpn
            Pause-Menu
        }

        "2" {
            Import-IkevProfile
            Pause-Menu
        }

        "3" {
            $profile = Select-Ikev2Profile -Action "view"
            if ($profile) {
                Show-VpnProfileStatus -Profile $profile
            }
            Pause-Menu
        }

        "4" {
            $profile = Select-Ikev2Profile -Action "connect"
            if ($profile) {
                Connect-VpnProfile -Profile $profile
            }
            Pause-Menu
        }

        "5" {
            $profile = Select-Ikev2Profile -Action "disconnect"
            if ($profile) {
                Disconnect-VpnProfile -Profile $profile
            }
            Pause-Menu
        }

        "6" {
            Configure-TrafficMode
            Pause-Menu
        }

        "7" {
            Write-Host ""
            Write-Host "Exiting..." -ForegroundColor Gray
            exit 0
        }

        default {
            Write-Warn "Invalid option."
            Start-Sleep -Seconds 1
        }
    }
}
