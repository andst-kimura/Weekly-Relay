@echo off
:: Weekly Relay 未対応チケット警告スクリプト
:: タスクスケジューラから毎朝呼び出される

cd /d "C:\Users\H016491\Weekly Relay"
"C:\Users\H016491\AppData\Local\Programs\Python\Python313\python.exe" main.py --run-alert >> output\run.log 2>&1
