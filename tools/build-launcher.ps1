$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) {
    throw 'The Windows .NET Framework C# compiler was not found.'
}
$output = Join-Path $projectRoot '字幕工具.exe'
$icon = Join-Path $projectRoot 'subtitle_app\icon.ico'
$source = Join-Path $PSScriptRoot 'launcher.cs'
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ "/win32icon:$icon" /reference:System.Windows.Forms.dll "/out:$output" $source
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed.' }
Write-Output "Built: $output"
