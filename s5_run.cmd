@echo off
rem ---------------------------------------------------------------
rem s5_run.cmd -- S5 command-line chat entry (wraps code\s5_chat.py).
rem Always uses the project venv python; no activation needed.
rem Works in cmd, PowerShell, and PyCharm Terminal.
rem
rem NOTE: keep this file pure ASCII. cmd.exe parses .cmd in the OEM
rem code page, so UTF-8 Chinese text here corrupts the whole script.
rem
rem Usage:
rem   s5_run.cmd "What is the weather in Beijing"
rem   s5_run.cmd --info probe-103045
rem   s5_run.cmd "now" --sid my-test
rem ---------------------------------------------------------------
"%~dp0.venv\Scripts\python.exe" "%~dp0code\s5_chat.py" %*
exit /b %errorlevel%
