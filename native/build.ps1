[CmdletBinding()]
param(
    [ValidateSet('Release', 'Debug', 'RelWithDebInfo')]
    [string] $Configuration = 'Release',
    [string] $BuildDirectory,
    [string] $CMakePath,
    [switch] $ConfigureOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ([string]::IsNullOrWhiteSpace($BuildDirectory)) {
    $BuildDirectory = Join-Path $PSScriptRoot 'build'
}

# Discover the existing compiler and bundled CMake; install nothing globally.
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
    throw 'Visual Studio Installer discovery tool was not found. Install the Visual Studio C++ build tools first.'
}
$visualStudio = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -version '[17.0,18.0)' -property installationPath
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($visualStudio)) {
    throw 'Visual Studio 2022 with the x64 C++ build tools was not found.'
}
$visualStudio = $visualStudio.Trim()
if ([string]::IsNullOrWhiteSpace($CMakePath)) {
    $bundledCMake = Join-Path $visualStudio 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
    if (Test-Path -LiteralPath $bundledCMake -PathType Leaf) {
        $CMakePath = $bundledCMake
    } else {
        $cmakeCommand = Get-Command 'cmake.exe' -ErrorAction SilentlyContinue
        if ($null -eq $cmakeCommand) { throw 'CMake was not found. Enable the Visual Studio C++ CMake tools or provide -CMakePath.' }
        $CMakePath = $cmakeCommand.Source
    }
}
if (-not (Test-Path -LiteralPath $CMakePath -PathType Leaf)) { throw "CMake not found: $CMakePath" }

$configureArguments = @(
    '-S', $PSScriptRoot, '-B', $BuildDirectory,
    '-G', 'Visual Studio 17 2022', '-A', 'x64',
    "-DCMAKE_GENERATOR_INSTANCE=$visualStudio"
)
& $CMakePath @configureArguments
if ($LASTEXITCODE -ne 0) { throw "CMake configuration failed (exit $LASTEXITCODE)." }
if ($ConfigureOnly) { return }

& $CMakePath --build $BuildDirectory --config $Configuration --target BilingualPanel --parallel
if ($LASTEXITCODE -ne 0) { throw "Native add-on build failed (exit $LASTEXITCODE)." }
$addon = Join-Path $BuildDirectory "bin\$Configuration\BilingualPanel.addon64"
if (-not (Test-Path -LiteralPath $addon -PathType Leaf)) { throw "Build completed without the expected add-on: $addon" }
Write-Output "Built: $((Resolve-Path -LiteralPath $addon).Path)"
Write-Output 'This command builds only. It does not install files into any game.'
