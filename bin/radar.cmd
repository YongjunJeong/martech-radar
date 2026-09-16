@echo off
rem Windows entry point. Same as bin/radar: run the CLI from the project's
rem own virtual environment without activating it.
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
"%ROOT%\.venv\Scripts\python.exe" -m radar.cli %*
