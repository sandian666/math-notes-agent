@echo off
rem S5 eval runner -- always uses the project venv python (same idea as p.cmd).
rem
rem Usage:
rem   eval.cmd                                    run all cases
rem   eval.cmd --limit 5                          run first 5 only (cheap smoke test)
rem   eval.cmd --cat rag                          run one category only
rem   eval.cmd --tag before                       name this run, for comparing two runs
rem   eval.cmd --compare eval\report_xxx.json     compare against a previous report
rem   eval.cmd --stats                            NO model calls: just summarize logs\run.jsonl
rem
rem NOTE: comments here are ASCII on purpose -- cmd.exe reads .cmd in the OEM code page.
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"%~dp0.venv\Scripts\python.exe" "%~dp0eval\run_eval.py" %*
exit /b %errorlevel%