param(
  [Parameter(Mandatory=$true)][string]$InputImage,
  [Parameter(Mandatory=$true)][string]$OutputJson
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime] | Out-Null

function Await-Operation($Operation, [Type]$ResultType) {
  $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
      $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 } |
    Select-Object -First 1
  $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
  return $task.GetAwaiter().GetResult()
}
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$inputPath = [IO.Path]::GetFullPath($InputImage)
$outputPath = [IO.Path]::GetFullPath($OutputJson)
if (-not $inputPath.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) -or
    -not $outputPath.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
  throw 'OCR paths must stay inside the project'
}
$language = [Windows.Globalization.Language]::new('zh-Hans-CN')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw 'Windows Chinese OCR language is unavailable' }
$file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($inputPath)) ([Windows.Storage.StorageFile])
$stream = Await-Operation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
try {
  $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $lines = @($result.Lines | ForEach-Object {
    $line = $_
    [ordered]@{
      text = $line.Text
      words = @($line.Words | ForEach-Object {
        [ordered]@{ text=$_.Text; x=$_.BoundingRect.X; y=$_.BoundingRect.Y;
                    width=$_.BoundingRect.Width; height=$_.BoundingRect.Height }
      })
    }
  })
  $record = [ordered]@{
    engine='windows_ocr'; language=$engine.RecognizerLanguage.LanguageTag;
    image_width=$decoder.PixelWidth; image_height=$decoder.PixelHeight;
    lines=$lines; confidence=$null; requires_review=$true
  }
  [IO.File]::WriteAllText($outputPath, ($record | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
} finally {
  $stream.Dispose()
}
