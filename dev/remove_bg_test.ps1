# Run with: powershell -NoProfile -File dev/remove_bg_test.ps1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$removeBg = Join-Path (Split-Path -Parent $PSScriptRoot) 'tools\remove-bg.ps1'
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$testRoot = Join-Path $tempRoot ('remove-bg-tests-' + [guid]::NewGuid().ToString('N'))
[void][System.IO.Directory]::CreateDirectory($testRoot)
$script:assertions = 0

function Assert-True([bool]$Condition, [string]$Message) {
    $script:assertions++
    if (-not $Condition) { throw $Message }
}

function New-TestBitmap([int]$Width, [int]$Height, [System.Drawing.Color]$Color) {
    $bitmap = New-Object System.Drawing.Bitmap($Width, $Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try { $graphics.Clear($Color) } finally { $graphics.Dispose() }
    return $bitmap
}

function Save-TestBitmap($Bitmap, [string]$Name) {
    $file = Join-Path $testRoot $Name
    try { $Bitmap.Save($file, [System.Drawing.Imaging.ImageFormat]::Png) }
    finally { $Bitmap.Dispose() }
    return $file
}

function Assert-Color($Pixel, $Expected, [string]$Message) {
    Assert-True ($Pixel.ToArgb() -eq $Expected.ToArgb()) $Message
}

try {
    # Two rings of known foreground coverage, composited against white.
    # Recovering alpha and foreground RGB prevents a white halo on dark backdrops.
    $foreground = [System.Drawing.Color]::FromArgb(40, 80, 120)
    $sprite = New-TestBitmap 11 11 ([System.Drawing.Color]::White)
    for ($y = 2; $y -le 8; $y++) {
        for ($x = 2; $x -le 8; $x++) {
            $coverage = if ($x -eq 2 -or $x -eq 8 -or $y -eq 2 -or $y -eq 8) { 0.25 }
                        elseif ($x -eq 3 -or $x -eq 7 -or $y -eq 3 -or $y -eq 7) { 0.75 }
                        else { 1.0 }
            $sprite.SetPixel($x, $y, [System.Drawing.Color]::FromArgb(
                [int][Math]::Round(255 * (1 - $coverage) + 40 * $coverage),
                [int][Math]::Round(255 * (1 - $coverage) + 80 * $coverage),
                [int][Math]::Round(255 * (1 - $coverage) + 120 * $coverage)))
        }
    }
    $sprite.SetPixel(5, 5, [System.Drawing.Color]::White)
    $inputPath = Save-TestBitmap $sprite 'antialiased.png'
    $outputPath = Join-Path $testRoot 'antialiased-out.png'
    & $removeBg -Path $inputPath -Out $outputPath | Out-Null
    $result = [System.Drawing.Bitmap]::new($outputPath)
    try {
        Assert-True ($result.Width -eq 11 -and $result.Height -eq 11) 'Image dimensions changed.'
        Assert-True ($result.GetPixel(0, 0).A -eq 0) 'Exterior background remains opaque.'
        foreach ($point in @(@(2, 5), @(8, 5), @(5, 2), @(5, 8), @(2, 2), @(8, 8))) {
            $pixel = $result.GetPixel($point[0], $point[1])
            Assert-True ([Math]::Abs([int]$pixel.A - 64) -le 2) 'Outer antialiased edge did not recover its coverage.'
            Assert-True ([Math]::Abs([int]$pixel.R - 40) -le 3 -and [Math]::Abs([int]$pixel.G - 80) -le 3 -and [Math]::Abs([int]$pixel.B - 120) -le 3) 'Background color remains in edge RGB.'
        }
        Assert-True ([Math]::Abs([int]$result.GetPixel(3, 5).A - 191) -le 2) 'Inner antialiased edge did not recover its coverage.'
        Assert-Color ($result.GetPixel(4, 5)) $foreground 'Solid foreground changed.'
        Assert-Color ($result.GetPixel(5, 5)) ([System.Drawing.Color]::White) 'Enclosed white sprite detail was removed.'
    } finally { $result.Dispose() }

    # Reprocessing a cutout must not keep thinning its antialiased outline.
    $secondPath = Join-Path $testRoot 'antialiased-twice.png'
    & $removeBg -Path $outputPath -Out $secondPath | Out-Null
    $first = [System.Drawing.Bitmap]::new($outputPath)
    $second = [System.Drawing.Bitmap]::new($secondPath)
    try {
        $same = $true
        for ($y = 0; $y -lt 11; $y++) {
            for ($x = 0; $x -lt 11; $x++) {
                if ($first.GetPixel($x, $y).ToArgb() -ne $second.GetPixel($x, $y).ToArgb()) { $same = $false }
            }
        }
        Assert-True $same 'Reprocessing the cutout changed its pixels.'
    } finally { $first.Dispose(); $second.Dispose() }

    $hardPath = Join-Path $testRoot 'hard-cut.png'
    & $removeBg -Path $inputPath -Out $hardPath -EdgeWidth 0 | Out-Null
    $result = [System.Drawing.Bitmap]::new($hardPath)
    try {
        Assert-Color ($result.GetPixel(2, 5)) ([System.Drawing.Color]::FromArgb(201, 211, 221)) 'EdgeWidth 0 changed edge color or alpha.'
        Assert-True ($result.GetPixel(0, 0).A -eq 0) 'EdgeWidth 0 did not remove the background.'
    } finally { $result.Dispose() }

    # Generated sprites can contain slightly tinted matte and alpha 253/254.
    $sprite = New-TestBitmap 9 9 ([System.Drawing.Color]::White)
    for ($y = 2; $y -le 6; $y++) {
        for ($x = 2; $x -le 6; $x++) {
            $color = if ($x -eq 2 -or $x -eq 6 -or $y -eq 2 -or $y -eq 6) {
                [System.Drawing.Color]::FromArgb(254, 202, 207, 206)
            } else { [System.Drawing.Color]::Black }
            $sprite.SetPixel($x, $y, $color)
        }
    }
    $inputPath = Save-TestBitmap $sprite 'noisy-matte.png'
    $outputPath = Join-Path $testRoot 'noisy-matte-out.png'
    & $removeBg -Path $inputPath -Out $outputPath | Out-Null
    $result = [System.Drawing.Bitmap]::new($outputPath)
    try {
        $pixel = $result.GetPixel(2, 4)
        Assert-True ($pixel.A -gt 0 -and $pixel.A -lt 100) 'Nearly opaque matte was not corrected.'
        Assert-True ($pixel.R -lt 30 -and $pixel.G -lt 30 -and $pixel.B -lt 30) 'Tinted matte still produces a light fringe.'
    } finally { $result.Dispose() }

    # A hard red edge against a blue interior is not a white/blue blend.
    $sprite = New-TestBitmap 9 9 ([System.Drawing.Color]::White)
    for ($y = 2; $y -le 6; $y++) {
        for ($x = 2; $x -le 6; $x++) {
            $color = if ($x -eq 2 -or $x -eq 6 -or $y -eq 2 -or $y -eq 6) {
                [System.Drawing.Color]::Red
            } else { [System.Drawing.Color]::Blue }
            $sprite.SetPixel($x, $y, $color)
        }
    }
    $inputPath = Save-TestBitmap $sprite 'hard-color.png'
    $outputPath = Join-Path $testRoot 'hard-color-out.png'
    & $removeBg -Path $inputPath -Out $outputPath | Out-Null
    $result = [System.Drawing.Bitmap]::new($outputPath)
    try {
        Assert-Color ($result.GetPixel(2, 4)) ([System.Drawing.Color]::Red) 'Hard colored outline was changed.'
        Assert-Color ($result.GetPixel(3, 4)) ([System.Drawing.Color]::Blue) 'Hard colored interior was changed.'
    } finally { $result.Dispose() }

    # A diagonal-only opening must connect to the exterior background.
    $sprite = New-TestBitmap 5 5 ([System.Drawing.Color]::Black)
    $sprite.SetPixel(0, 0, [System.Drawing.Color]::White)
    $sprite.SetPixel(1, 1, [System.Drawing.Color]::White)
    $sprite.SetPixel(3, 3, [System.Drawing.Color]::White)
    $inputPath = Save-TestBitmap $sprite 'diagonal.png'
    $outputPath = Join-Path $testRoot 'diagonal-out.png'
    & $removeBg -Path $inputPath -Out $outputPath | Out-Null
    $result = [System.Drawing.Bitmap]::new($outputPath)
    try {
        Assert-True ($result.GetPixel(1, 1).A -eq 0) 'Diagonal background gap was not removed.'
        Assert-Color ($result.GetPixel(3, 3)) ([System.Drawing.Color]::White) 'Enclosed white detail changed.'
        Assert-Color ($result.GetPixel(1, 0)) ([System.Drawing.Color]::Black) 'One-pixel outline changed.'
    } finally { $result.Dispose() }

    # Already transparent borders are exterior seeds regardless of hidden RGB.
    $sprite = New-TestBitmap 7 7 ([System.Drawing.Color]::FromArgb(0, 0, 0, 0))
    $sprite.SetPixel(2, 3, [System.Drawing.Color]::White)
    $sprite.SetPixel(3, 3, [System.Drawing.Color]::FromArgb(100, 40, 80, 120))
    $sprite.SetPixel(4, 3, [System.Drawing.Color]::FromArgb(128, 240, 240, 240))
    $inputPath = Save-TestBitmap $sprite 'alpha.png'
    $outputPath = Join-Path $testRoot 'alpha-out.png'
    & $removeBg -Path $inputPath -Out $outputPath | Out-Null
    $result = [System.Drawing.Bitmap]::new($outputPath)
    try {
        Assert-True ($result.GetPixel(2, 3).A -eq 0) 'Background beside transparent pixels was not removed.'
        Assert-Color ($result.GetPixel(3, 3)) ([System.Drawing.Color]::FromArgb(100, 40, 80, 120)) 'Existing partial alpha or color changed.'
        Assert-Color ($result.GetPixel(4, 3)) ([System.Drawing.Color]::FromArgb(128, 240, 240, 240)) 'Translucent white artwork was removed.'
    } finally { $result.Dispose() }

    # Tiny images and filename-only paths must work in Windows PowerShell 5.1.
    $inputPath = Save-TestBitmap (New-TestBitmap 1 1 ([System.Drawing.Color]::White)) 'tiny.png'
    Push-Location $testRoot
    try {
        & $removeBg -Path 'tiny.png' | Out-Null
        Assert-True (Test-Path -LiteralPath 'tiny_transparent.png') 'Default output path is incorrect.'
        & $removeBg -Path 'tiny.png' -Out 'chosen.png' | Out-Null
        Assert-True (Test-Path -LiteralPath 'chosen.png') 'Explicit output filename was ignored.'
    } finally { Pop-Location }
    $result = [System.Drawing.Bitmap]::new((Join-Path $testRoot 'chosen.png'))
    try { Assert-True ($result.GetPixel(0, 0).A -eq 0) 'Tiny image background was not removed.' }
    finally { $result.Dispose() }

    # Indexed PNGs use a padded byte stride on disk, not packed ARGB rows.
    $sprite = [System.Drawing.Bitmap]::new(3, 3, [System.Drawing.Imaging.PixelFormat]::Format8bppIndexed)
    $palette = $sprite.Palette
    $palette.Entries[0] = [System.Drawing.Color]::White
    $palette.Entries[1] = [System.Drawing.Color]::Red
    $sprite.Palette = $palette
    $rect = [System.Drawing.Rectangle]::new(0, 0, 3, 3)
    $data = $sprite.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::WriteOnly, $sprite.PixelFormat)
    try {
        $bytes = New-Object byte[] ($data.Stride * 3)
        $bytes[$data.Stride + 1] = 1
        [System.Runtime.InteropServices.Marshal]::Copy($bytes, 0, $data.Scan0, $bytes.Length)
    } finally { $sprite.UnlockBits($data) }
    $inputPath = Save-TestBitmap $sprite 'indexed.png'
    $outputPath = Join-Path $testRoot 'indexed-out.png'
    & $removeBg -Path $inputPath -Out $outputPath | Out-Null
    $result = [System.Drawing.Bitmap]::new($outputPath)
    try {
        Assert-True ($result.GetPixel(0, 0).A -eq 0) 'Indexed image background was not removed.'
        Assert-Color ($result.GetPixel(1, 1)) ([System.Drawing.Color]::Red) 'Indexed image foreground changed.'
    } finally { $result.Dispose() }

    $inputPath = Save-TestBitmap (New-TestBitmap 2 2 ([System.Drawing.Color]::FromArgb(220, 220, 220))) 'threshold.png'
    $outputPath = Join-Path $testRoot 'threshold-out.png'
    & $removeBg -Path $inputPath -Out $outputPath -Threshold 225 | Out-Null
    $result = [System.Drawing.Bitmap]::new($outputPath)
    try { Assert-True ($result.GetPixel(0, 0).A -eq 255) 'Threshold removed a pixel outside the selected range.' }
    finally { $result.Dispose() }
    $outputPath = Join-Path $testRoot 'threshold-200-out.png'
    & $removeBg -Path $inputPath -Out $outputPath -Threshold 200 | Out-Null
    $result = [System.Drawing.Bitmap]::new($outputPath)
    try { Assert-True ($result.GetPixel(0, 0).A -eq 0) 'Threshold override was ignored.' }
    finally { $result.Dispose() }

    $rejected = $false
    try { & $removeBg -Path $inputPath -Out $inputPath | Out-Null }
    catch { $rejected = $_.Exception.Message -like '*different from the input*' }
    Assert-True $rejected 'The script did not reject overwriting its input.'
    $rejected = $false
    try { & $removeBg -Path $inputPath -Out $outputPath -Threshold 256 | Out-Null }
    catch { $rejected = $_.FullyQualifiedErrorId -like 'ParameterArgumentValidationError*' }
    Assert-True $rejected 'An invalid threshold was accepted.'

    Write-Output "PASS: $script:assertions remove-bg assertions."
} finally {
    $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
    if (-not $resolvedTestRoot.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $resolvedTestRoot) -notlike 'remove-bg-tests-*') {
        throw "Refusing to remove unexpected test directory: $resolvedTestRoot"
    }
    Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
}
