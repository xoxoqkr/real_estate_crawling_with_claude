@echo off
REM 입력 폴더를 지켜보다가 스피드옥션 엑셀이 들어오면 자동으로 매칭합니다.
REM 작업 스케줄러에 "로그온할 때" 실행으로 등록하면 PC를 켤 때마다 자동으로 시작됩니다.
chcp 65001 > nul
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe match_auctions.py --watch >> watch.log 2>&1
) else (
  python match_auctions.py --watch >> watch.log 2>&1
)
