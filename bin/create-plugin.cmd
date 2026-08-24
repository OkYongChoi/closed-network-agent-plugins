@echo off
python "%~dp0create-plugin" %*
exit /b %errorlevel%
