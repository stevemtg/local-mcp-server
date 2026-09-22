<#
.SYNOPSIS
Reports the alpha transparency of an image without modifying anything.
.DESCRIPTION
Loads any image supported by System.Drawing, scans every pixel, and reports
the source PixelFormat, whether that format or palette carries alpha, the
count of fully transparent (A=0), partially transparent (0<A<255), and fully
opaque (A=255) pixels, the four corner pixel colors, and the bounding box of
all pixels with A>0. Read-only: the script never writes files and never
alters the inspected image. A fully transparent or opaque image is reported,
not repaired. Requires Windows and System.Drawing; works with Windows
PowerShell 5.1. Runs on CPU with no GPU, ComfyUI, or Ollama dependency.
.PARAMETER SrcPath
Source image path. Path is an alias. Relative paths use the current directory.
.PARAMETER RequestPath
JSON request file used by the MCP adapter. Fields: image_path. Produces JSON
metadata on stdout.
.PARAMETER AsJson
Return machine-readable metadata instead of human-readable lines.
.EXAMPLE
.\tools\check-transparency.ps1 -Path .\sprite.png
.EXAMPLE
.\tools\check-transparency.ps1 -Path .\sprite.png -AsJson
#>
[CmdletBinding(DefaultParameterSetName='Image')]
param(
    [Parameter(Mandatory=$true, Position=0, ParameterSetName='Image')][Alias('Path')][string]$SrcPath,
    [Parameter(Mandatory=$true, ParameterSetName='Request')][string]$RequestPath,
    [switch]$AsJson
)

$ErrorActionPreference = 'Stop'
if ($PSCmdlet.ParameterSetName -eq 'Request') {
    $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    & $PSCommandPath -SrcPath $request.image_path -AsJson
    return
}
if ($AsJson) { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) }
$sourcePath = (Resolve-Path -LiteralPath $SrcPath).ProviderPath

. (Join-Path $PSScriptRoot 'image-alpha.ps1')
$report = [SpriteAlphaInspector]::Inspect($sourcePath)
$alpha = $report.alpha
$total = [long]$alpha.pixels
$transparentFraction = [Math]::Round(100.0 * [long]$alpha.transparent / $total, 1)
$opaqueFraction = [Math]::Round(100.0 * [long]$alpha.opaque / $total, 1)
if ($AsJson) {
    [ordered]@{
        status = 'succeeded'
        source = [ordered]@{
            path = $sourcePath; width = $report.width; height = $report.height
            pixel_format = $report.pixel_format; alpha_channel = [bool]$report.alpha_channel
        }
        corners = $report.corners
        alpha = $alpha
        content_bounds = $report.content_bounds
    } | ConvertTo-Json -Depth 6 -Compress
} else {
    Write-Output "Source: $sourcePath ($($report.width)x$($report.height))"
    Write-Output "PixelFormat: $($report.pixel_format) (alpha channel: $($report.alpha_channel))"
    foreach ($corner in @('top_left', 'top_right', 'bottom_left', 'bottom_right')) {
        $p = $report.corners.$corner
        $label = (Get-Culture).TextInfo.ToTitleCase($corner.Replace('_', ''))
        Write-Output "${label}: A=$($p.a) R=$($p.r) G=$($p.g) B=$($p.b)"
    }
    Write-Output "Fully transparent pixels: $($alpha.transparent) of $total = $transparentFraction%"
    Write-Output "Partially transparent: $($alpha.partial); fully opaque: $($alpha.opaque) = $opaqueFraction%"
    if ($null -ne $report.content_bounds) {
        $b = $report.content_bounds
        Write-Output "Content bounds (A>0): x=$($b.x) y=$($b.y) width=$($b.width) height=$($b.height)"
    } else {
        Write-Output 'Content bounds (A>0): none (fully transparent image)'
    }
}


