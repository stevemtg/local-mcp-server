<#
.SYNOPSIS
Exports enlarged image regions as PNGs for visual inspection.
.DESCRIPTION
Crops one or more named regions from any image supported by System.Drawing.
Coordinates start at the top-left. Omitting Regions zooms the entire image.
The script produces inspection images; it does not detect malformed anatomy.
Requires Windows and System.Drawing; works with Windows PowerShell 5.1.
.PARAMETER SrcPath
Source image path. Path is an alias. Relative paths use the current directory.
.PARAMETER Regions
Dictionary mapping output labels to @(X, Y, Width, Height). Use [ordered]@{}
to control processing order. Labels must be valid filename components.
.PARAMETER Units
Relative (default) uses fractions of the source dimensions, between 0 and 1.
Crop boundaries round outward to include intersected pixels. Pixels requires
integer coordinates and sizes. All regions must fit inside the source image.
Each call supports at most 8 regions and 16 million output pixels in total.
.PARAMETER Scale
Positive zoom factor, default 3. Output dimensions round to the nearest pixel,
with a minimum of one pixel. Use 1 for an unscaled crop.
.PARAMETER OutDir
Output directory, created if needed. Defaults to a 'crops' folder beside the
source. Files are named <source-stem>_<region>.png; reruns replace those files.
.PARAMETER Interpolation
NearestNeighbor (default) preserves visible pixel boundaries. Choose
HighQualityBilinear or HighQualityBicubic for smoothly enlarged previews.
.PARAMETER RequestPath
JSON request file used by the MCP adapter. Fields: image_path, output_dir,
regions, units, scale, interpolation. Produces JSON metadata on stdout.
.PARAMETER AsJson
Return machine-readable source/crop metadata instead of progress messages.
.EXAMPLE
.\tools\crop-zoom.ps1 -SrcPath .\image.png -Scale 2
.EXAMPLE
.\tools\crop-zoom.ps1 -Path .\image.png -Regions @{ face = @(0.3, 0.1, 0.4, 0.3) } -Scale 4
.EXAMPLE
$regions = [ordered]@{ left = @(100, 300, 160, 180); right = @(600, 300, 160, 180) }
.\tools\crop-zoom.ps1 -Path .\image.png -Regions $regions -Units Pixels -OutDir .\inspection -Scale 3
#>
[CmdletBinding(DefaultParameterSetName='Image')]
param(
    [Parameter(Mandatory=$true, Position=0, ParameterSetName='Image')][Alias('Path')][string]$SrcPath,
    [Parameter(Mandatory=$true, ParameterSetName='Request')][string]$RequestPath,
    [string]$OutDir,
    [System.Collections.IDictionary]$Regions,
    [ValidateSet('Relative', 'Pixels')][string]$Units = 'Relative',
    [double]$Scale = 3,
    [ValidateSet('NearestNeighbor', 'HighQualityBilinear', 'HighQualityBicubic')]
    [string]$Interpolation = 'NearestNeighbor',
    [switch]$AsJson
)

$ErrorActionPreference = 'Stop'
if ($PSCmdlet.ParameterSetName -eq 'Request') {
    $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $options = @{ SrcPath = $request.image_path }
    foreach ($pair in @(@('output_dir', 'OutDir'), @('units', 'Units'), @('scale', 'Scale'), @('interpolation', 'Interpolation'))) {
        if ($null -ne $request.($pair[0])) { $options[$pair[1]] = $request.($pair[0]) }
    }
    if ($null -ne $request.regions) {
        $options.Regions = [ordered]@{}
        foreach ($property in $request.regions.PSObject.Properties) {
            if ($options.Regions.Contains($property.Name)) { throw 'Region labels must be unique, ignoring case.' }
            $options.Regions[$property.Name] = $property.Value
        }
    }
    & $PSCommandPath @options -AsJson
    return
}
if ($AsJson) { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) }
if ([double]::IsNaN($Scale) -or [double]::IsInfinity($Scale) -or $Scale -le 0) {
    throw 'Scale must be a finite number greater than zero.'
}
if ($PSBoundParameters.ContainsKey('Regions') -and ($null -eq $Regions -or $Regions.Count -eq 0)) {
    throw 'Regions must contain at least one named crop, or be omitted to zoom the whole image.'
}
if ($null -ne $Regions -and $Regions.Count -gt 8) { throw 'Use at most 8 regions per call.' }
$sourcePath = (Resolve-Path -LiteralPath $SrcPath).ProviderPath
$sourceName = [System.IO.Path]::GetFileNameWithoutExtension($sourcePath)
if (-not $OutDir) {
    $OutDir = Join-Path ([System.IO.Path]::GetDirectoryName($sourcePath)) 'crops'
}
$outputDirectory = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutDir)

Add-Type -AssemblyName System.Drawing
$src = [System.Drawing.Bitmap]::new($sourcePath)
try {
    $w = $src.Width
    $h = $src.Height
    if (-not $AsJson) { Write-Output "Source: $sourcePath (${w}x${h})" }
    if ($null -eq $Regions) {
        $Regions = if ($Units -eq 'Pixels') { [ordered]@{ full = @(0, 0, $w, $h) } }
                   else { [ordered]@{ full = @(0, 0, 1, 1) } }
    }

    # Validate every crop before creating any output, so a bad later region
    # does not leave an incomplete batch of inspection images.
    $totalPixels = 0.0
    $plans = @(foreach ($name in $Regions.Keys) {
        if ($name -isnot [string] -or [string]::IsNullOrWhiteSpace($name) -or
            $name.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0 -or $name -in '.', '..') {
            throw "Invalid region label '$name': use a nonempty filename component."
        }
        $values = @($Regions[$name])
        if ($values.Count -ne 4) { throw "Region '$name' must contain X, Y, Width, Height." }
        $r = @(foreach ($value in $values) {
            if ($null -eq $value -or $value -is [bool]) { throw "Region '$name' coordinates must be numbers." }
            try { $number = [double]$value }
            catch { throw "Region '$name' coordinates must be numbers." }
            if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) {
                throw "Region '$name' coordinates must be finite numbers."
            }
            $number
        })
        if ($r[0] -lt 0 -or $r[1] -lt 0 -or $r[2] -le 0 -or $r[3] -le 0) {
            throw "Region '$name' requires nonnegative X/Y and positive Width/Height."
        }
        if ($Units -eq 'Relative') {
            if ($r[0] + $r[2] -gt 1.000000001 -or $r[1] + $r[3] -gt 1.000000001) {
                throw "Region '$name' extends outside the source image; relative X+Width and Y+Height must be at most 1."
            }
            # The epsilon avoids an extra pixel caused by floating-point sums
            # such as 0.1 + 0.2. Clamp the rounded endpoints to the image bounds.
            $x = [int][Math]::Floor($w * $r[0] + 1e-9)
            $y = [int][Math]::Floor($h * $r[1] + 1e-9)
            $right = [int][Math]::Min($w, [Math]::Ceiling($w * ($r[0] + $r[2]) - 1e-9))
            $bottom = [int][Math]::Min($h, [Math]::Ceiling($h * ($r[1] + $r[3]) - 1e-9))
        } else {
            foreach ($number in $r) {
                if ($number -ne [Math]::Truncate($number)) { throw "Region '$name' must use integer pixel coordinates." }
            }
            if ($r[0] + $r[2] -gt $w -or $r[1] + $r[3] -gt $h) {
                throw "Region '$name' extends outside the ${w}x${h} source image."
            }
            $x = [int]$r[0]
            $y = [int]$r[1]
            $right = [int]($r[0] + $r[2])
            $bottom = [int]($r[1] + $r[3])
        }
        $rect = [System.Drawing.Rectangle]::new($x, $y, $right - $x, $bottom - $y)
        if ($rect.Width -le 0 -or $rect.Height -le 0) { throw "Region '$name' is too small to cover a pixel." }
        $outputWidth = [Math]::Max(1, [Math]::Round($rect.Width * $Scale, [MidpointRounding]::AwayFromZero))
        $outputHeight = [Math]::Max(1, [Math]::Round($rect.Height * $Scale, [MidpointRounding]::AwayFromZero))
        if ($outputWidth -gt [int]::MaxValue -or $outputHeight -gt [int]::MaxValue) {
            throw "Region '$name' is too large at scale $Scale."
        }
        $totalPixels += $outputWidth * $outputHeight
        if ($totalPixels -gt 16000000) { throw 'Crops exceed 16 million output pixels; reduce the regions or Scale.' }
        [pscustomobject]@{
            Name = $name
            Rectangle = $rect
            Width = [int]$outputWidth
            Height = [int]$outputHeight
            OutputPath = Join-Path $outputDirectory ($sourceName + '_' + $name + '.png')
        }
    })

    [void][System.IO.Directory]::CreateDirectory($outputDirectory)
    $results = @(foreach ($plan in $plans) {
        $crop = $bitmap = $graphics = $attributes = $null
        try {
            $crop = $src.Clone($plan.Rectangle, $src.PixelFormat)
            $bitmap = [System.Drawing.Bitmap]::new($plan.Width, $plan.Height, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]$Interpolation
            $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
            $attributes = [System.Drawing.Imaging.ImageAttributes]::new()
            $attributes.SetWrapMode([System.Drawing.Drawing2D.WrapMode]::TileFlipXY)
            $destination = [System.Drawing.Rectangle]::new(0, 0, $bitmap.Width, $bitmap.Height)
            $graphics.DrawImage($crop, $destination, 0, 0, $crop.Width, $crop.Height, [System.Drawing.GraphicsUnit]::Pixel, $attributes)
            $bitmap.Save($plan.OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
            [pscustomobject]@{
                name = $plan.Name
                path = $plan.OutputPath
                crop = [ordered]@{ x = $plan.Rectangle.X; y = $plan.Rectangle.Y; width = $plan.Rectangle.Width; height = $plan.Rectangle.Height }
                width = $plan.Width
                height = $plan.Height
            }
        } finally {
            if ($null -ne $attributes) { $attributes.Dispose() }
            if ($null -ne $graphics) { $graphics.Dispose() }
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            if ($null -ne $crop) { $crop.Dispose() }
        }
    })
    if ($AsJson) {
        [ordered]@{
            status = 'succeeded'
            source = [ordered]@{ path = $sourcePath; width = $w; height = $h }
            output_dir = $outputDirectory
            images = @($results)
        } | ConvertTo-Json -Depth 6 -Compress
    } else {
        foreach ($result in $results) { Write-Output "Wrote $($result.path) ($($result.width)x$($result.height))" }
    }
} finally { $src.Dispose() }
