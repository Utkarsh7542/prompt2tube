@echo off
cd /d %~dp0
if not exist .env (
  echo No .env file found. Copy .env.example to .env and fill in your keys.
  pause
  exit /b 1
)
for /f "usebackq tokens=* eol=#" %%a in (".env") do set "%%a"
python app.py
