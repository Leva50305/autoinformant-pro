@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import pygame, mutagen, psutil, imageio_ffmpeg, ttkbootstrap, PIL, yandex_music" >nul 2>&1
if errorlevel 1 (
  echo Installing AutoInformant Pro dependencies...
  py -3 -m pip install --upgrade pip
  py -3 -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Failed to install dependencies. Install Python 3.10+ and run: py -3 -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)
py -3 AutoInformant.py
if errorlevel 1 pause
