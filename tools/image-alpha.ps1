# Shared, read-only pixel inspection for the two image tools.
Add-Type -AssemblyName System.Drawing
if (-not ('SpriteAlphaInspector' -as [type])) {
Add-Type -TypeDefinition @'
using System;

using System.Collections.Specialized;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public static class SpriteAlphaInspector
{
    private static OrderedDictionary Pixel(int bgra)
    {
        OrderedDictionary d = new OrderedDictionary();
        d["a"] = (int)((uint)bgra >> 24);
        d["r"] = (bgra >> 16) & 255;
        d["g"] = (bgra >> 8) & 255;
        d["b"] = (int)(bgra & 255);
        return d;
    }

    public static OrderedDictionary Inspect(string path)
    {
        using (Bitmap bmp = new Bitmap(path))
        {
            int w = bmp.Width, h = bmp.Height;
            if (w <= 0 || h <= 0) throw new InvalidOperationException("Image has no pixels.");
            PixelFormat original = bmp.PixelFormat;
            bool alphaChannel = (original & PixelFormat.Alpha) != 0;
            // Indexed PNG/GIF transparency lives in the palette rather than
            // the pixel format's Alpha flag.
            if ((original & PixelFormat.Indexed) != 0)
                foreach (Color color in bmp.Palette.Entries)
                    if (color.A < 255) { alphaChannel = true; break; }
            BitmapData data = bmp.LockBits(
                new Rectangle(0, 0, w, h), ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
            long transparent = 0, partial = 0, opaque = 0;
            int minAlpha = 255, maxAlpha = 0;
            int minX = w, minY = h, maxX = -1, maxY = -1;
            OrderedDictionary corners = new OrderedDictionary();
            try
            {
                corners["top_left"] = Pixel(Marshal.ReadInt32(data.Scan0, 0));
                corners["top_right"] = Pixel(Marshal.ReadInt32(data.Scan0, (w - 1) * 4));
                corners["bottom_left"] = Pixel(Marshal.ReadInt32(data.Scan0, (h - 1) * data.Stride));
                corners["bottom_right"] = Pixel(Marshal.ReadInt32(data.Scan0, (h - 1) * data.Stride + (w - 1) * 4));
                for (int y = 0; y < h; y++)
                {
                    IntPtr row = IntPtr.Add(data.Scan0, y * data.Stride);
                    for (int x = 0; x < w; x++)
                    {
                        int a = (int)((uint)Marshal.ReadInt32(IntPtr.Add(row, x * 4)) >> 24);
                        if (a == 0) transparent++;
                        else if (a == 255) opaque++;
                        else partial++;
                        if (a < minAlpha) minAlpha = a;
                        if (a > maxAlpha) maxAlpha = a;
                        if (a > 0)
                        {
                            if (x < minX) minX = x;
                            if (x > maxX) maxX = x;
                            if (y < minY) minY = y;
                            if (y > maxY) maxY = y;
                        }
                    }
                }
            }
            finally { bmp.UnlockBits(data); }

            OrderedDictionary alpha = new OrderedDictionary();
            alpha["pixels"] = (long)w * h;
            alpha["transparent"] = transparent;
            alpha["partial"] = partial;
            alpha["opaque"] = opaque;
            alpha["min_alpha"] = (transparent > 0) ? 0 : minAlpha;
            alpha["max_alpha"] = maxAlpha;
            alpha["transparent_fraction"] = (double)transparent / ((long)w * h);
            alpha["partial_fraction"] = (double)partial / ((long)w * h);
            alpha["opaque_fraction"] = (double)opaque / ((long)w * h);

            OrderedDictionary result = new OrderedDictionary();
            result["path"] = path;
            result["width"] = w;
            result["height"] = h;
            result["pixel_format"] = original.ToString();
            result["alpha_channel"] = alphaChannel;
            result["corners"] = corners;
            result["alpha"] = alpha;
            if (maxX < minX || maxY < minY) result["content_bounds"] = null;
            else
            {
                OrderedDictionary bounds = new OrderedDictionary();
                bounds["x"] = minX;
                bounds["y"] = minY;
                bounds["width"] = maxX - minX + 1;
                bounds["height"] = maxY - minY + 1;
                result["content_bounds"] = bounds;
            }
            return result;
        }
    }
}
'@ -ReferencedAssemblies System.Drawing
}
