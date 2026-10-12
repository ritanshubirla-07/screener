@echo off
REM run_daily_equity_pipeline.bat
REM Runs the unified daily and quarterly Indian Equity master pipeline

cd /d "c:\Users\hiten\mf-feature"
python run_daily_equity_pipeline.py %*
