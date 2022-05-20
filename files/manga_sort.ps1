$filepath = "./manga_tracking.csv"
$temp = Get-Content -Encoding "utf8" -Path $filepath
$temp[0] | Set-Content -Encoding "utf8" -Path $filepath
$temp[1..$temp.Length] | Sort-Object | Add-Content -Encoding "utf8" -Path $filepath