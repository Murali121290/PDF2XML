@echo off
rem pdf2xml (deploy edition): PDF -> Docling -> canonical JSON/XML -> BITS -> JATS, offline.
rem Usage: convert.bat <file.pdf | folder> [more PDFs or folders] [options]
rem        convert.bat --help
setlocal
set "PDF2XML_HOME=%~dp0"
if "%PDF2XML_HOME:~-1%"=="\" set "PDF2XML_HOME=%PDF2XML_HOME:~0,-1%"

rem Everything comes from this folder: the models, and no network access for model files.
if not defined PDF2XML_MODELS_DIR set "PDF2XML_MODELS_DIR=%PDF2XML_HOME%\models"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "HF_DATASETS_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HOME=%PDF2XML_HOME%\cache\huggingface"
set "TORCH_HOME=%PDF2XML_HOME%\cache\torch"

if "%~1"=="" (
    "%PDF2XML_HOME%\python\python.exe" -I -X utf8 -m pdf2xml --help
    echo.
    echo Drag PDF files or a folder onto convert.bat, or run: convert.bat C:\path\book.pdf
    pause
    exit /b 2
)

"%PDF2XML_HOME%\python\python.exe" -I -X utf8 -m pdf2xml %*
exit /b %ERRORLEVEL%
