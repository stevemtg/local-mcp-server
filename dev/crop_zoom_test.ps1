# Run with: powershell -NoProfile -ExecutionPolicy Bypass -File dev/crop_zoom_test.ps1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'tools\crop-zoom.ps1'
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$testRoot = Join-Path $tempRoot ('crop-zoom-tests-' + [guid]::NewGuid().ToString('N'))
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
                $source.SetPixel($x, $y, [System.Drawing.Color]::FromArgb(255, $x * 20, $y * 20, 80))
            }
        }
        $source.SetPixel(2, 2, [System.Drawing.Color]::FromArgb(128, 80, 120, 160))
        $source.SetPixel(3, 2, [System.Drawing.Color]::FromArgb(0, 0, 0, 0))
        $source.Save($sourcePath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally { $source.Dispose() }

    $result = & $scriptPath -Path $sourcePath -AsJson | ConvertFrom-Json
    Assert-True ($result.source.width -eq 10 -and $result.source.height -eq 8) 'Source dimensions were not reported.'
    Assert-True ($result.images.Count -eq 1) 'Omitted regions did not create one full-image crop.'
    Assert-True ($result.images[0].width -eq 30 -and $result.images[0].height -eq 24) 'Default scale or full-image size is incorrect.'
    Assert-True ($result.output_dir -eq (Join-Path $testRoot 'crops')) 'Default output directory is not beside the source.'
    Assert-True (Test-Path -LiteralPath $result.images[0].path) 'Default PNG was not written.'

    $regions = [ordered]@{ detail = @(2, 2, 3, 2); corner = @(9, 7, 1, 1) }
    $result = & $scriptPath -SrcPath $sourcePath -Regions $regions -Units Pixels -Scale 2 -AsJson | ConvertFrom-Json
    Assert-True ($result.images.Count -eq 2 -and $result.images[0].name -eq 'detail') 'Named region order changed.'
    Assert-True ($result.images[0].crop.x -eq 2 -and $result.images[0].crop.width -eq 3) 'Pixel crop metadata is incorrect.'
    $crop = [System.Drawing.Bitmap]::new($result.images[0].path)
    try {
        Assert-True ($crop.Width -eq 6 -and $crop.Height -eq 4) 'Pixel crop dimensions are incorrect.'
        $pixel = $crop.GetPixel(0, 0)
        Assert-True ($pixel.A -eq 128) 'Partial transparency was not preserved.'
        Assert-True ([Math]::Abs([int]$pixel.R - 80) -le 1 -and [Math]::Abs([int]$pixel.G - 120) -le 1) 'Transparent pixel color changed.'
        Assert-True ($crop.GetPixel(2, 0).A -eq 0) 'Fully transparent pixel became opaque.'
        Assert-True ($crop.GetPixel(4, 2).ToArgb() -eq [System.Drawing.Color]::FromArgb(255, 80, 60, 80).ToArgb()) 'Nearest-neighbor crop selected the wrong source pixel.'
    } finally { $crop.Dispose() }

    $result = & $scriptPath -Path $sourcePath -Regions @{ relative = @(0.2, 0.25, 0.3, 0.25) } -Scale 1.5 -AsJson | ConvertFrom-Json
    Assert-True ($result.images[0].crop.x -eq 2 -and $result.images[0].crop.y -eq 2 -and $result.images[0].crop.width -eq 3 -and $result.images[0].crop.height -eq 2) 'Relative crop does not match pixel crop.'
    Assert-True ($result.images[0].width -eq 5 -and $result.images[0].height -eq 3) 'Fractional zoom did not round to nearest pixel.'
    $result = & $scriptPath -Path $sourcePath -Regions @{ rounded = @(0.11, 0.14, 0.1, 0.1) } -Scale 1 -AsJson | ConvertFrom-Json
    Assert-True ($result.images[0].crop.x -eq 1 -and $result.images[0].crop.y -eq 1 -and $result.images[0].crop.width -eq 2 -and $result.images[0].crop.height -eq 1) 'Fractional crop bounds did not round outward.'
    foreach ($mode in @('HighQualityBilinear', 'HighQualityBicubic')) {
        $result = & $scriptPath -Path $sourcePath -Units Pixels -Scale 1 -Interpolation $mode -AsJson | ConvertFrom-Json
        Assert-True ($result.images[0].width -eq 10 -and $result.images[0].height -eq 8) "Whole-image pixel crop failed with $mode."
    }

    # Invalid later regions must not leave earlier outputs behind.
    $invalidDir = Join-Path $testRoot 'invalid'
    $cases = @(
        @{ Regions = [ordered]@{ good = @(0, 0, 0.1, 0.1); bad = @(0.9, 0, 0.2, 1) } },
        @{ Regions = @{ bad = @(-0.1, 0, 1, 1) } },
        @{ Regions = @{ bad = @(0, 0, 0, 1) } },
        @{ Regions = @{ bad = @(0, 0, [double]::NaN, 1) } },
        @{ Regions = @{ bad = @(0, 0, 1) } },
        @{ Regions = @{ '../escape' = @(0, 0, 1, 1) } },
        @{ Regions = @{ bad = @(0.5, 0, 1, 1) }; Units = 'Pixels' },
        @{ Regions = @{} },
        @{ Scale = 0 },
        @{ Scale = [double]::PositiveInfinity },
        @{ Scale = 1000 }
    )
    foreach ($options in $cases) {
        $rejected = $false
        try { & $scriptPath -Path $sourcePath -OutDir $invalidDir -AsJson @options | Out-Null }
        catch { $rejected = $true }
        Assert-True $rejected 'Invalid crop arguments were accepted.'
        Assert-True (-not (Test-Path -LiteralPath $invalidDir)) 'Invalid batch created an output directory.'
    }
    # Confirm every invocation released its source image handle.
    $stream = [System.IO.File]::Open($sourcePath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    $stream.Dispose()
    Assert-True $true 'Source image was released.'
    Write-Output "PASS: $script:assertions crop-zoom assertions."
} finally {
    $resolved = [System.IO.Path]::GetFullPath($testRoot)
    if (-not $resolved.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $resolved) -notlike 'crop-zoom-tests-*') { throw "Unexpected test directory: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
