$filepath = "./manga_tracking.csv"
Move-Item $filepath "temp.file"
Get-Content -Encoding "utf8" -Path "temp.file" | Sort-Object | Out-File -FilePath $filepath
Remove-Item "temp.file"