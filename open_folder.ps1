param([string]$url)
$path = $url -replace '^opendir:///', ''
$path = [System.Uri]::UnescapeDataString($path).TrimEnd('/') -replace '/', '\'
Start-Process explorer.exe -ArgumentList $path
