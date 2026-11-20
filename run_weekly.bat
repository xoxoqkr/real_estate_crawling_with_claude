@echo off
REM 스피드옥션 수집 + 정비구역 매칭을 한 번 실행합니다. 작업 스케줄러에 "매주 월요일" 등으로 등록하세요.
chcp 65001 > nul
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python.exe) else (set PY=python)
%PY% sa_collect.py >> collect.log 2>&1
