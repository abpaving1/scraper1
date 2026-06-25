@echo off
echo ================================================
echo     Tippster Scraper - Full Stack Starter
echo ================================================

:: Activate virtual environment
echo [1/5] Activating virtual environment...
call .venv\Scripts\activate.bat

:: Start Docker services
echo [2/5] Starting Redis and Postgres...
docker compose up -d redis postgres

:: Wait for services
echo [3/5] Waiting for services to be ready...
timeout /t 10 /nobreak >nul

:: Copy and run schema if needed
echo [4/5] Initializing database schema...
docker compose cp sql/schema.sql postgres:/tmp/schema.sql 2>nul
docker compose exec postgres psql -U postgres -d tippster -f /tmp/schema.sql >nul 2>&1 || echo Schema already initialized or minor error.

echo [5/5] Starting Processor...
start "Tippster - Processor" cmd /k "title Tippster - Processor && .venv\Scripts\activate.bat && python -m processor.consumer"

echo.
echo ================================================
echo All services started!
echo.
echo Available commands:
echo   python cli.py olbg
echo   python cli.py forebet
echo   python cli.py freesupertips
echo   python cli.py soccervista
echo.
echo Press any key to open a scraper terminal...
pause >nul

start "Tippster - Scraper" cmd /k "title Tippster - Scraper && .venv\Scripts\activate.bat && echo Ready! Try: python cli.py olbg"
echo.
echo Enjoy building smart accas! ⚽
