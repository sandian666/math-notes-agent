@echo off
rem ---------------------------------------------------------------
rem s5_eval.cmd -- S5 eval runner (wraps eval\run_eval.py).
rem Always uses the project venv python; no activation needed.
rem
rem NOTE: keep this file pure ASCII. cmd.exe parses .cmd in the OEM
rem code page, so UTF-8 Chinese text here corrupts the whole script.
rem
rem Usage:
rem   s5_eval.cmd                                 run all cases
rem   s5_eval.cmd --limit 5                       run first 5 only (cheap smoke test)
rem   s5_eval.cmd --cat rag                       run one category only
rem   s5_eval.cmd --tag before                    name this run, for comparing two runs
rem   s5_eval.cmd --compare eval\report_xxx.json  compare against a previous report
rem   s5_eval.cmd --stats                         NO model calls: summarize logs\run.jsonl
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%~dp0.venv\Scripts\python.exe" "%~dp0eval\run_eval.py" %*
exit /b %errorlevel%
