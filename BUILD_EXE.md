# Build the Windows EXE

Run this from the project root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt; if ($LASTEXITCODE -eq 0) { .\.venv\Scripts\python.exe -m PyInstaller --onefile --clean --name regional_market_report regional_market_report.py }
```

The executable will be created at:

```text
dist\regional_market_report.exe
```

When the EXE runs, it writes raw cached data beside the EXE under `cache\` and report files under `output\`:

```text
cache\regional_raw.json
output\regional_report.md
output\regional_report.pdf
output\regional_report.png
```
