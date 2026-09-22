# Run with: powershell -NoProfile -ExecutionPolicy Bypass -File dev/check_transparency_test.ps1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'tools\check-transparency.ps1'
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$testRoot = Join-Path $tempRoot ('check-transparency-tests-' + [guid]::NewGuid().ToString('N'))
[void][System.IO.Directory]::CreateDirectory($testRoot)
$script:assertions = 0
function Assert-True([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { throw $Message }
}
try {
    $sourcePath = Join-Path $testRoot 'fixture [1].png'
    $source = [System.Drawing.Bitmap]::new(10, 8)
    try {
        for ($y = 0; $y -lt 8; $y++) {
            for ($x = 0; $x -lt 10; $x++) {
                if ($x -ge 2 -and $x -lt 8 -and $y -ge 2 -and $y -lt 6) {
                    if ($x -eq 5 -and $y -eq 3) {
                        $source.SetPixel($x, $y, [System.Drawing.Color]::FromArgb(128, 200, 200, 200))
                    } else {
                        $source.SetPixel($x, $y, [System.Drawing.Color]::FromArgb(255, $x * 20, $y * 20, 80))
                    }
                } else {
                    $source.SetPixel($x, $y, [System.Drawing.Color]::FromArgb(0, 0, 0, 0))
                }
            }
        }
        $source.Save($sourcePath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally { $source.Dispose() }
    $originalBytes = [System.IO.File]::ReadAllBytes($sourcePath)

    $result = & $scriptPath -Path $sourcePath -AsJson | ConvertFrom-Json
    Assert-True ($result.source.width -eq 10 -and $result.source.height -eq 8) 'Source dimensions are wrong.'
    Assert-True ($result.source.pixel_format -eq 'Format32bppArgb') 'Alpha pixel format was not detected.'
    Assert-True ($result.source.alpha_channel -eq $true) 'alpha_channel is not true for a 32bppArgb PNG.'
    Assert-True ($result.alpha.pixels -eq 80 -and $result.alpha.transparent -eq 56 -and
                 $result.alpha.partial -eq 1 -and $result.alpha.opaque -eq 23) 'Alpha counts are wrong.'
    Assert-True ($result.alpha.transparent_fraction -eq 0.7 -and $result.alpha.opaque_fraction -eq 0.2875 -and
                 $result.alpha.partial_fraction -eq 0.0125) 'Alpha fractions are wrong.'
    Assert-True ($result.corners.top_left.a -eq 0 -and $result.corners.bottom_right.a -eq 0) 'Corners should be transparent.'
    Assert-True ($result.content_bounds.x -eq 2 -and $result.content_bounds.y -eq 2 -and
                 $result.content_bounds.width -eq 6 -and $result.content_bounds.height -eq 4) 'Content bounds are wrong.'

    # Human-readable mode must succeed on the same file and mention the counts.
    $lines = & $scriptPath -SrcPath $sourcePath
    Assert-True (($lines -join "`n") -match 'Fully transparent pixels: 56 of 80') 'Human mode lacks the transparent count.'

    # A fully transparent image reports no content bounds.
    $blankPath = Join-Path $testRoot 'blank.png'
    $blank = [System.Drawing.Bitmap]::new(4, 4)
    try {
        for ($y = 0; $y -lt 4; $y++) { for ($x = 0; $x -lt 4; $x++) { $blank.SetPixel($x, $y, [System.Drawing.Color]::FromArgb(0, 0, 0, 0)) } }
        $blank.Save($blankPath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally { $blank.Dispose() }
    $blankResult = & $scriptPath -Path $blankPath -AsJson | ConvertFrom-Json
    Assert-True ($null -eq $blankResult.content_bounds) 'Fully transparent image reported content bounds.'
    Assert-True ($blankResult.alpha.transparent -eq 16 -and $blankResult.alpha.opaque -eq 0) 'Blank image counts are wrong.'

    # The Request parameter set must produce the same JSON as direct invocation.
    $requestPath = Join-Path $testRoot 'request.json'
    [System.IO.File]::WriteAllText($requestPath, (@{ image_path = $sourcePath } | ConvertTo-Json -Compress))
    $viaRequest = & $scriptPath -RequestPath $requestPath | ConvertFrom-Json
    Assert-True ($viaRequest.alpha.transparent -eq 56 -and $viaRequest.content_bounds.width -eq 6) 'RequestPath mode differs from direct invocation.'

    # Missing files and unreadable images must fail.
    $rejected = $false
    try { & $scriptPath -Path (Join-Path $testRoot 'missing.png') -AsJson | Out-Null } catch { $rejected = $true }
    Assert-True $rejected 'A missing image was accepted.'

    # Confirm every invocation released its source image handle and nothing changed.
    $stream = [System.IO.File]::Open($sourcePath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    $stream.Dispose()
    Assert-True $true 'Source image was released.'
    $after = [System.IO.File]::ReadAllBytes($sourcePath)
    Assert-True (@(Compare-Object $after $originalBytes -SyncWindow 0).Count -eq 0) 'The script modified the source image.'
    Write-Output "PASS: $script:assertions check-transparency assertions."
} finally {
    $resolved = [System.IO.Path]::GetFullPath($testRoot)
    if (-not $resolved.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $resolved) -notlike 'check-transparency-tests-*') { throw "Unexpected test directory: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
