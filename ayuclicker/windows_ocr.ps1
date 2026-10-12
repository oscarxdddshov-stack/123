$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Write-Response($value) {
    [Console]::WriteLine(($value | ConvertTo-Json -Depth 5 -Compress))
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    Add-Type -AssemblyName System.Drawing
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AyuScreenDpi {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@
    $null = [AyuScreenDpi]::SetProcessDPIAware()
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapPixelFormat, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapAlphaMode, Windows.Foundation, ContentType=WindowsRuntime]
    $script:awaitMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
        $_.GetGenericArguments().Length -eq 1 -and $_.GetParameters().Length -eq 1
    } | Select-Object -First 1

    function Await-Result($operation, [Type] $resultType) {
        $method = $script:awaitMethod.MakeGenericMethod($resultType)
        $task = $method.Invoke($null, @($operation))
        $task.Wait()
        return $task.Result
    }

    $configuration = [Console]::ReadLine() | ConvertFrom-Json
    if ($configuration.language) {
        $language = [Windows.Globalization.Language]::new([string]$configuration.language)
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    } else {
        $russian = [Windows.Globalization.Language]::new('ru-RU')
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($russian)
        if ($null -eq $engine) {
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        }
    }
    if ($null -eq $engine) {
        throw 'OCR language unavailable. Install Text recognition (OCR) for Russian or English in Windows Settings > Language options.'
    }
    Write-Response @{ ready = $true; language = $engine.RecognizerLanguage.LanguageTag }

    function Read-Crop($screen, $region, [int] $left, [int] $top) {
        if ($region.width -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
            $region.height -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) {
            throw 'Selected region is too large for Windows OCR.'
        }
        $rectangle = [System.Drawing.Rectangle]::new(
            [int]($region.x - $left), [int]($region.y - $top), [int]$region.width, [int]$region.height
        )
        $crop = $screen.Clone($rectangle, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $memory = New-Object System.IO.MemoryStream
        $randomAccess = $null
        $software = $null
        try {
            $crop.Save($memory, [System.Drawing.Imaging.ImageFormat]::Png)
            $memory.Position = 0
            $randomAccess = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($memory)
            $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($randomAccess)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $software = Await-Result ($decoder.GetSoftwareBitmapAsync(
                [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
                [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied
            )) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $result = Await-Result ($engine.RecognizeAsync($software)) ([Windows.Media.Ocr.OcrResult])
            return $result.Text
        } finally {
            if ($null -ne $software) { $software.Dispose() }
            if ($null -ne $randomAccess) { $randomAccess.Dispose() }
            $memory.Dispose()
            $crop.Dispose()
        }
    }

    while ($null -ne ($line = [Console]::ReadLine())) {
        try {
            $request = $line | ConvertFrom-Json
            $left = [Math]::Min($request.text.x, $request.button.x)
            $top = [Math]::Min($request.text.y, $request.button.y)
            $right = [Math]::Max(($request.text.x + $request.text.width), ($request.button.x + $request.button.width))
            $bottom = [Math]::Max(($request.text.y + $request.text.height), ($request.button.y + $request.button.height))
            $bitmap = [System.Drawing.Bitmap]::new([int]($right - $left), [int]($bottom - $top))
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.CopyFromScreen($left, $top, 0, 0, $bitmap.Size)
                $text = Read-Crop $bitmap $request.text $left $top
                $button = Read-Crop $bitmap $request.button $left $top
                Write-Response @{ text = $text; button = $button }
            } finally {
                $graphics.Dispose()
                $bitmap.Dispose()
            }
        } catch {
            Write-Response @{ error = $_.Exception.Message }
        }
    }
} catch {
    Write-Response @{ error = $_.Exception.Message }
    exit 1
}
