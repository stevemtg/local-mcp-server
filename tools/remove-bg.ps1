<#
.SYNOPSIS
Removes a white background from a sprite and cleans white matte from its outline.
.DESCRIPTION
Removes near-white background connected to the image border, including diagonal
gaps. In a narrow band along that background, estimates antialias coverage from
nearby foreground colors and removes the white contribution from RGB as well as
alpha. Enclosed white details and translucent artwork are preserved. Almost
opaque pixels (alpha 250-255) are eligible for cleanup; alpha is never increased.
Intended for sprites on a flat white/near-white background, not photographs or
textured backgrounds. Fully enclosed background holes cannot be distinguished
from white sprite details and are preserved.
.PARAMETER Threshold
Minimum R, G, and B values considered background (0-255, default 225).
.PARAMETER EdgeWidth
Width in pixels of the edge band to clean (default 2). Increase for a wider
matte fringe; use 0 for a hard cutout with no edge color correction.
.PARAMETER Out
Output PNG path. Defaults to <input-name>_transparent.png beside the input.
.EXAMPLE
.\tools\remove-bg.ps1 -Path .\sprite.png -Out .\sprite-transparent.png
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Path,
    [string]$Out,
    [ValidateRange(0, 255)][int]$Threshold = 225,
    [ValidateRange(0, 16)][int]$EdgeWidth = 2
)

$ErrorActionPreference = 'Stop'
$inputPath = (Resolve-Path -LiteralPath $Path).ProviderPath
if (-not $Out) {
    $Out = Join-Path ([System.IO.Path]::GetDirectoryName($inputPath)) (
        [System.IO.Path]::GetFileNameWithoutExtension($inputPath) + '_transparent.png')
}
$outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Out)
if ([string]::Equals($inputPath, $outputPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The output path must be different from the input path.'
}

if (-not ('SpriteBackgroundRemover' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public static class SpriteBackgroundRemover
{
    private static IEnumerable<int> Neighbors(int i, int w, int h)
    {
        int x = i % w, y = i / w;
        for (int dy = -1; dy <= 1; dy++)
            for (int dx = -1; dx <= 1; dx++)
                if ((dx != 0 || dy != 0) && x + dx >= 0 && x + dx < w && y + dy >= 0 && y + dy < h)
                    yield return (y + dy) * w + x + dx;
    }

    private static int Channel(double value)
    {
        return Math.Max(0, Math.Min(255, (int)Math.Round(value)));
    }

    private static int CleanEdge(int i, int[] pixels, int[] distance, int w, int h, int radius)
    {
        int c = pixels[i];
        // Preserve existing translucency. Allow slight alpha noise in almost
        // opaque inputs, otherwise isolated 253/254-alpha matte pixels survive.
        int sourceAlpha = (int)((uint)c >> 24);
        if (sourceAlpha < 250) return c;
        double cr = 255 - ((c >> 16) & 255);
        double cg = 255 - ((c >> 8) & 255);
        double cb = 255 - (c & 255);
        double length = cr * cr + cg * cg + cb * cb;
        double bestLength = length, bestAlpha = 1.0;
        int x = i % w, y = i / w;

        // For a white matte, C = alpha * F + (1-alpha) * 255. Search
        // slightly inside the silhouette for F on the same color line.
        // A color mismatch is left untouched, protecting sharp colored edges.
        for (int sy = Math.Max(0, y - radius); sy <= Math.Min(h - 1, y + radius); sy++)
        {
            for (int sx = Math.Max(0, x - radius); sx <= Math.Min(w - 1, x + radius); sx++)
            {
                int n = sy * w + sx;
                if (distance[n] <= distance[i] || (uint)pixels[n] >> 24 < 250) continue;
                int f = pixels[n];
                double fr = 255 - ((f >> 16) & 255);
                double fg = 255 - ((f >> 8) & 255);
                double fb = 255 - (f & 255);
                double flength = fr * fr + fg * fg + fb * fb;
                if (flength <= length) continue;
                double alpha = (cr * fr + cg * fg + cb * fb) / flength;
                if (alpha <= 0 || alpha >= 0.98) continue;
                double er = cr - alpha * fr, eg = cg - alpha * fg, eb = cb - alpha * fb;
                // Allow small color noise in the matte (common in generated
                // sprites). Prefer the strongest compatible reference rather
                // than mistaking another fringe pixel for solid foreground.
                if (Math.Max(Math.Abs(er), Math.Max(Math.Abs(eg), Math.Abs(eb))) <= 12 &&
                    flength > bestLength)
                {
                    bestLength = flength;
                    bestAlpha = alpha;
                }
            }
        }

        if (bestAlpha == 1.0) return c;
        int a = Channel(sourceAlpha * bestAlpha);
        if (a == 0) return 0;
        return (a << 24) | (Channel(255 - cr / bestAlpha) << 16) |
            (Channel(255 - cg / bestAlpha) << 8) | Channel(255 - cb / bestAlpha);
    }

    public static string Remove(string path, string outPath, int threshold, int edgeWidth)
    {
        using (var bmp = new Bitmap(path))
        {
            int w = bmp.Width, h = bmp.Height;
            var rect = new Rectangle(0, 0, w, h);
            int[] buf = new int[w * h];
            var data = bmp.LockBits(rect, ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
            try {
                for (int y = 0; y < h; y++)
                    Marshal.Copy(IntPtr.Add(data.Scan0, y * data.Stride), buf, y * w, w);
            } finally { bmp.UnlockBits(data); }

            bool[] bg = new bool[buf.Length];
            var queue = new Queue<int>();

            // Transparent pixels are background regardless of their hidden RGB,
            // so this also cleans sprites processed by the old hard-cut tool.
            Func<int, bool> isBg = i =>
            {
                int c = buf[i];
                int b = (c) & 0xFF, g = (c >> 8) & 0xFF, r = (c >> 16) & 0xFF;
                uint alpha = (uint)c >> 24;
                return alpha == 0 || (alpha >= 250 && r >= threshold && g >= threshold && b >= threshold);
            };
            for (int x = 0; x < w; x++)
            {
                int t = x, btm = (h - 1) * w + x;
                if (!bg[t] && isBg(t)) { bg[t] = true; queue.Enqueue(t); }
                if (!bg[btm] && isBg(btm)) { bg[btm] = true; queue.Enqueue(btm); }
            }
            for (int y = 0; y < h; y++)
            {
                int l = y * w, right = y * w + w - 1;
                if (!bg[l] && isBg(l)) { bg[l] = true; queue.Enqueue(l); }
                if (!bg[right] && isBg(right)) { bg[right] = true; queue.Enqueue(right); }
            }

            // Flood fill from border seeds
            while (queue.Count > 0)
            {
                int i = queue.Dequeue();
                foreach (int n in Neighbors(i, w, h))
                    if (!bg[n] && isBg(n)) { bg[n] = true; queue.Enqueue(n); }
            }

            // Measure distance into the foreground. Only the narrow exterior
            // band is eligible for unmatting; the sprite interior is unchanged.
            int[] distance = new int[buf.Length];
            for (int i = 0; i < buf.Length; i++)
                distance[i] = bg[i] ? 0 : edgeWidth + 1;
            if (edgeWidth > 0)
            {
                for (int i = 0; i < buf.Length; i++)
                    if (bg[i])
                        foreach (int n in Neighbors(i, w, h))
                            if (distance[n] > 1) { distance[n] = 1; queue.Enqueue(n); }
                while (queue.Count > 0)
                {
                    int i = queue.Dequeue();
                    if (distance[i] >= edgeWidth) continue;
                    foreach (int n in Neighbors(i, w, h))
                        if (distance[n] > distance[i] + 1)
                        {
                            distance[n] = distance[i] + 1;
                            queue.Enqueue(n);
                        }
                }
            }

            // Always estimate from the original pixels, never from already
            // corrected neighbors (which would make results scan-order dependent).
            int[] output = (int[])buf.Clone();
            for (int i = 0; i < buf.Length; i++)
            {
                if (bg[i]) output[i] = 0;
                else if (distance[i] <= edgeWidth)
                    output[i] = CleanEdge(i, buf, distance, w, h, edgeWidth + 2);
            }

            using (var outBmp = new Bitmap(w, h, PixelFormat.Format32bppArgb))
            {
                var outData = outBmp.LockBits(rect, ImageLockMode.WriteOnly, PixelFormat.Format32bppArgb);
                try {
                    for (int y = 0; y < h; y++)
                        Marshal.Copy(output, y * w, IntPtr.Add(outData.Scan0, y * outData.Stride), w);
                } finally { outBmp.UnlockBits(outData); }
                outBmp.Save(outPath, ImageFormat.Png);
            }
            return outPath;
        }
    }
}
'@ -ReferencedAssemblies System.Drawing
}

$result = [SpriteBackgroundRemover]::Remove($inputPath, $outputPath, $Threshold, $EdgeWidth)
Write-Output "Saved: $result"
