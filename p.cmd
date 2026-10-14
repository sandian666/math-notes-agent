@echo off
rem S5 short command wrapper -- always uses the project venv python.
rem No activation needed. Works in cmd, PowerShell, and PyCharm Terminal.
rem
rem Usage:
rem   p.cmd "What is the weather in Beijing"
rem   p.cmd --info probe-103045
rem   p.cmd "now" --sid my-test
rem
rem NOTE: comments in this file are ASCII on purpose -- cmd.exe reads .cmd
rem files in the OEM code page, so UTF-8 Chinese comments can break parsing.

"%~dp0.venv\Scripts\python.exe" "%~dp0probe_run.py" %*
exit /b %errorlevel%