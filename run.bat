@echo off
cd /d "%~dp0"
echo ========================================
echo   SKU Label Generator
echo ========================================
echo.
echo Installing dependencies...
pip install -r requirements.txt -q
echo.
echo Starting server at http://localhost:5000
echo Press Ctrl+C to stop.
echo.
python app.py
pause
