@echo off
rem Double-click to open Reelm Encoder in your browser. Close this window to stop it.
title Reelm Encoder
cd /d "%~dp0"
uv run reelm-encoder ui || pause
