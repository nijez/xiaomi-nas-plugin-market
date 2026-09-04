[CmdletBinding()]
param(
    [string]$NasIp = $env:NAS_IP,
    [string]$NasSshKey = $env:NAS_SSH_KEY,
    [string]$NasUserId = $env:NAS_USER_ID,
    [int]$PluginId = 11002
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Program,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE"
    }
}

function Invoke-NasSsh {
    param([Parameter(Mandatory = $true)][string]$Command)
    $output = & ssh.exe @script:SshOptions $script:Remote $Command
    if ($LASTEXITCODE -ne 0) {
        throw "NAS command failed with exit code $LASTEXITCODE"
    }
    return $output
}

function Find-XiaomiCertificateUsers {
    param([string[]]$RegistryUsers)
    $roots = @(
        (Join-Path $env:LOCALAPPDATA "Programs"),
        (Join-Path $env:APPDATA "Xiaomi"),
        (Join-Path $env:ProgramData "Xiaomi")
    ) | Where-Object { $_ -and (Test-Path $_) }
    $matches = New-Object System.Collections.Generic.List[string]
    foreach ($root in $roots) {
        Get-ChildItem -LiteralPath $root -Filter "*_cert.pem" -File -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.BaseName -match '^(\d+)_.*_cert$') {
                $candidate = "u$($Matches[1])"
                if ($RegistryUsers -contains $candidate -and -not $matches.Contains($candidate)) {
                    $matches.Add($candidate)
                }
            }
        }
    }
    return $matches.ToArray()
}

if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue) -or -not (Get-Command scp.exe -ErrorAction SilentlyContinue)) {
    throw "未找到 Windows OpenSSH 客户端。请在 Windows 设置的可选功能中安装 OpenSSH 客户端。"
}

if ([string]::IsNullOrWhiteSpace($NasIp)) {
    $NasIp = Read-Host "请输入小米 NAS 当前局域网 IP"
}
$parsedIp = $null
if (-not [System.Net.IPAddress]::TryParse($NasIp, [ref]$parsedIp) -or $parsedIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
    throw "NAS IP 格式不正确：$NasIp"
}

if ([string]::IsNullOrWhiteSpace($NasSshKey)) {
    $keyCandidates = @(
        (Join-Path $HOME ".xiaomi-nas-root\nas-root-key"),
        (Join-Path $HOME ".ssh\xiaomi-nas-root"),
        (Join-Path $HOME ".ssh\nas-root-key")
    )
    $NasSshKey = $keyCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($NasSshKey)) {
    $NasSshKey = Read-Host "未自动找到密钥，请输入 root SSH 私钥完整路径"
    $NasSshKey = $NasSshKey.Trim('"')
}
if (-not (Test-Path -LiteralPath $NasSshKey -PathType Leaf)) {
    throw "SSH 私钥不存在：$NasSshKey"
}
if ($PluginId -lt 1) {
    throw "插件 ID 必须是正整数"
}

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Remote = "root@$NasIp"
$SshOptions = @(
    "-i", $NasSshKey,
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "PreferredAuthentications=publickey",
    "-o", "PubkeyAuthentication=yes",
    "-o", "PasswordAuthentication=no",
    "-o", "KbdInteractiveAuthentication=no",
    "-o", "StrictHostKeyChecking=accept-new"
)

Invoke-NasSsh "command -v python3 >/dev/null && command -v openssl >/dev/null && command -v nginx >/dev/null && command -v systemctl >/dev/null" | Out-Null

if ([string]::IsNullOrWhiteSpace($NasUserId)) {
    $registryOutput = Invoke-NasSsh 'for path in /data/plugin/u*.list; do [ -f "$path" ] || continue; name=${path##*/}; printf "%s\n" "${name%.list}"; done'
    $registryUsers = @($registryOutput | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^u[A-Za-z0-9_-]+$' } | Select-Object -Unique)
    if ($registryUsers.Count -eq 1) {
        $NasUserId = $registryUsers[0]
    } else {
        $certUsers = @(Find-XiaomiCertificateUsers -RegistryUsers $registryUsers)
        if ($certUsers.Count -eq 1) {
            $NasUserId = $certUsers[0]
        } else {
            Write-Host "检测到多个小米账号："
            for ($index = 0; $index -lt $registryUsers.Count; $index++) {
                Write-Host "  $($index + 1). $($registryUsers[$index])"
            }
            $selection = 0
            if (-not [int]::TryParse((Read-Host "请输入当前账号前面的序号"), [ref]$selection) -or $selection -lt 1 -or $selection -gt $registryUsers.Count) {
                throw "账号序号无效"
            }
            $NasUserId = $registryUsers[$selection - 1]
        }
    }
}
if ($NasUserId -notmatch '^u[A-Za-z0-9_-]+$') {
    throw "小米用户 ID 无效：$NasUserId"
}
Write-Host "使用小米用户：$NasUserId"

$required = @(
    "server.py",
    "storelib.py",
    "web\index.html",
    "web\assets\community-store-v4.png",
    "catalog\catalog.json",
    "catalog\catalog.json.sig",
    "catalog\repository-public.pem",
    "deploy\xiaomi-community-store.service",
    "deploy\xiaomi-community-store.nginx.conf",
    "deploy\register_plugin.py"
)
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir $relative) -PathType Leaf)) {
        throw "发布包缺少文件：$relative"
    }
}

$releaseId = "0.1.1-$(Get-Date -Format yyyyMMddHHmmss)"
$ScpOptions = @($SshOptions)
$sshVersion = (& cmd.exe /c "ssh.exe -V 2>&1" | Out-String)
if ($sshVersion -match 'OpenSSH(?:_for_Windows)?_(\d+)' -and [int]$Matches[1] -ge 9) {
    $ScpOptions += "-O"
}
$remoteRelease = "/data/plugin/community-store/releases/$releaseId"
$temporaryService = Join-Path ([System.IO.Path]::GetTempPath()) "xiaomi-community-store-$PID.service"
try {
    $urlUserId = $NasUserId.Substring(1)
    $service = (Get-Content -LiteralPath (Join-Path $ProjectDir "deploy\xiaomi-community-store.service") -Raw).Replace("__NAS_USER_ID__", $NasUserId).Replace("__URL_USER_ID__", $urlUserId)
    [System.IO.File]::WriteAllText($temporaryService, $service, (New-Object System.Text.UTF8Encoding($false)))

    Invoke-NasSsh "mkdir -p '$remoteRelease' /data/plugin/community-store/state /data/plugin/community-store/staging /data/plugin/community-store/backups /data/plugin/www/icon" | Out-Null
    foreach ($item in @("server.py", "storelib.py", "web", "catalog")) {
        $source = Join-Path $ProjectDir $item
        Invoke-Native -Program "scp.exe" -Arguments ($ScpOptions + @("-r", $source, "${Remote}:$remoteRelease/"))
    }
    Invoke-Native -Program "scp.exe" -Arguments ($ScpOptions + @($temporaryService, "${Remote}:/tmp/xiaomi-community-store.service"))
    Invoke-Native -Program "scp.exe" -Arguments ($ScpOptions + @((Join-Path $ProjectDir "deploy\xiaomi-community-store.nginx.conf"), "${Remote}:/tmp/xiaomi-community-store.conf"))
    Invoke-Native -Program "scp.exe" -Arguments ($ScpOptions + @((Join-Path $ProjectDir "deploy\register_plugin.py"), "${Remote}:/tmp/register-community-store.py"))
    Invoke-Native -Program "scp.exe" -Arguments ($ScpOptions + @((Join-Path $ProjectDir "web\assets\community-store-v4.png"), "${Remote}:/data/plugin/www/icon/community-store-v4.icon"))

    $sessionSecret = ((Invoke-NasSsh 'set -eu; token=/data/plugin/community-store/admin-token; if [ ! -s "$token" ]; then umask 077; openssl rand -hex 16 > "$token"; fi; chmod 600 "$token"; cat "$token"') -join "").Trim()
    if ($sessionSecret -notmatch '^[0-9a-f]{32}$') {
        throw "NAS 未返回有效会话密钥"
    }
    Invoke-NasSsh "openssl dgst -sha256 -verify '$remoteRelease/catalog/repository-public.pem' -signature '$remoteRelease/catalog/catalog.json.sig' '$remoteRelease/catalog/catalog.json' >/dev/null" | Out-Null
    Invoke-NasSsh "set -eu; service=/etc/systemd/system/xiaomi-community-store.service; conf=/etc/nginx/conf.d/luci/xiaomi-community-store.conf; stamp=`$(date +%s); [ ! -f `"`$service`" ] || cp -p `"`$service`" `"`$service.before-`$stamp.bak`"; [ ! -f `"`$conf`" ] || cp -p `"`$conf`" `"`$conf.before-`$stamp.bak`"; install -m 0644 /tmp/xiaomi-community-store.service `"`$service`"; install -m 0644 /tmp/xiaomi-community-store.conf `"`$conf`"; ln -sfn '$remoteRelease' /data/plugin/community-store/current; nginx -t; systemctl daemon-reload; systemctl enable xiaomi-community-store.service; systemctl restart xiaomi-community-store.service; systemctl reload nginx" | Out-Null
    Invoke-NasSsh "python3 /tmp/register-community-store.py --user-id '$NasUserId' --plugin-id '$PluginId'" | Out-Null
    Invoke-NasSsh "systemctl is-active --quiet xiaomi-community-store.service && python3 -c `"import urllib.request; urllib.request.urlopen('http://127.0.0.1:18119/healthz', timeout=8)`"" | Out-Null

    Write-Host ""
    Write-Host "插件市场已安装。请完全退出并重新打开小米智能存储客户端。"
    Write-Host "插件市场会使用小米客户端入口自动授权，不需要管理码。"
} finally {
    Remove-Item -LiteralPath $temporaryService -Force -ErrorAction SilentlyContinue
}
