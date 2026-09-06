param([string]$Installer)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Invoke-NasSsh', 'Find-XiaomiCertificateUsers')) {
    $definition = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    . ([scriptblock]::Create($definition.Extent.Text))
}
$script:SshOptions = @()
$script:Remote = 'fixture-only'
function ssh.exe {
    $wire = $args[-1]
    if ($wire.Contains('"') -or $wire.Contains("'")) { throw 'Quotes reached native transport' }
    # Exercise PowerShell native argument handling, then an actual POSIX shell.
    $received = & /bin/echo $wire
    $result = & /bin/sh -c $received
    $script:LASTEXITCODE = $LASTEXITCODE
    return $result
}
$root = Join-Path ([IO.Path]::GetTempPath()) ('nas-windows-test-' + [guid]::NewGuid().ToString('N'))
try {
    $env:LOCALAPPDATA = Join-Path $root 'local'
    $env:APPDATA = Join-Path $root 'roaming'
    $env:ProgramData = Join-Path $root 'programdata'
    $certs = Join-Path $env:LOCALAPPDATA 'Programs'
    [void][IO.Directory]::CreateDirectory($certs)
    foreach ($name in @('123_456_cert.pem', '123_789_cert.pem', '987_654_cert.pem')) {
        [IO.File]::WriteAllText((Join-Path $certs $name), 'fixture')
    }
    $users = @(Find-XiaomiCertificateUsers -RegistryUsers @('u123', 'u987'))
    if (($users | Sort-Object) -join ',' -ne 'u123,u987') { throw 'Certificate selection failed' }
    foreach ($mode in @('Standard', 'Legacy')) {
        $PSNativeCommandArgumentPassing = $mode
        $output = @(Invoke-NasSsh 'set -eu; name="u123"; printf "%s\n" "$name" "u987"; printf "%s\n" "space and \"quote\""')
        if (($output -join '|') -ne 'u123|u987|space and "quote"') { throw "Roundtrip failed: $mode" }
        $failed = $false
        try { Invoke-NasSsh 'exit 17' } catch {
            if ($_.Exception.Message -notmatch 'exit code 17') { throw }
            $failed = $true
        }
        if (-not $failed) { throw 'Remote nonzero exit was lost' }
    }
    Write-Output 'PASS: certificate deduplication, Standard/Legacy quoting and nonzero exits'
} finally {
    Remove-Item -LiteralPath $root -Force -Recurse -ErrorAction SilentlyContinue
}
