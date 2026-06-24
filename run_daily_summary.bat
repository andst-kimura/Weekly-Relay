@echo off
:: Weekly Relay 日次夕方サマリースクリプト
:: タスクスケジューラから毎夕呼び出される

cd /d "C:\Users\H016491\00_行動分析ツール作成"
"C:\Users\H016491\AppData\Local\Programs\Python\Python313\python.exe" main.py --run-summary >> output\run.log 2>&1
