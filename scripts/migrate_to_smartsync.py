"""
Wasabi Firestore → SmartSync Firestore 移行スクリプト

Wasabi の context_snapshots（weekly-relay プロジェクト）を
SmartSync 形式に変換して smart-sync-stg へ書き込む。

使い方:
  python scripts/migrate_to_smartsync.py --dry-run   # 変換結果のみ表示（書き込みなし）
  python scripts/migrate_to_smartsync.py             # 実際に移行
  python scripts/migrate_to_smartsync.py --limit 5   # 先頭5件のみ（動作確認用）
"""
import sys
import os
import re
import argparse
import json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import truststore
truststore.inject_into_ssl()

from src.firestore_client import FirestoreClient
import src.smartsync_client as sc
from src.smartsync_convert import convert, WASABI_PROJECT_ID



# --------------------------------------------------------------------------- #
#  メイン
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Wasabi → SmartSync データ移行")
    parser.add_argument("--dry-run", action="store_true", help="変換結果のみ表示（書き込みなし）")
    parser.add_argument("--limit", type=int, default=0, help="移行件数の上限（0=全件）")
    args = parser.parse_args()

    print(f"=== Wasabi → SmartSync 移行 {'[DRY-RUN]' if args.dry_run else ''} ===")
    print(f"移行先DB: {sc._database()}")
    print(f"project_id: {WASABI_PROJECT_ID}")
    print()

    # Wasabi Firestore から全ドキュメントを読み込む
    print("Wasabi Firestore から読み込み中...")
    fs_client = FirestoreClient()
    wasabi_docs = fs_client.list_context_snapshots(page_size=300)
    print(f"  取得: {len(wasabi_docs)} 件")
    print()

    if args.limit > 0:
        wasabi_docs = wasabi_docs[:args.limit]
        print(f"--limit {args.limit} 件に制限")
        print()

    # 変換
    converted = []
    skipped = []
    for wasabi_doc_id, wasabi_data in wasabi_docs:
        result = convert(wasabi_doc_id, wasabi_data)
        if result is None:
            skipped.append(wasabi_doc_id)
        else:
            converted.append(result)

    print(f"変換結果: {len(converted)} 件変換可 / {len(skipped)} 件スキップ")
    if skipped:
        print(f"  スキップ: {skipped}")
    print()

    # dry-run は変換結果だけ表示して終了
    if args.dry_run:
        print("--- 変換結果プレビュー（先頭5件）---")
        for doc_id, data in converted[:5]:
            print(f"\n[{doc_id}]")
            preview = {k: (v[:100] + "..." if isinstance(v, str) and len(v) > 100 else v)
                       for k, v in data.items()}
            print(json.dumps(preview, ensure_ascii=False, indent=2))
        print("\n--dry-run: SmartSync への書き込みはスキップしました。")
        return

    # SmartSync へ書き込み
    ok = 0
    err = 0
    for i, (doc_id, data) in enumerate(converted, 1):
        try:
            sc.save_context_snapshot(doc_id, data)
            print(f"  [{i}/{len(converted)}] OK: {doc_id}")
            ok += 1
        except Exception as e:
            print(f"  [{i}/{len(converted)}] NG: {doc_id} → {e}")
            err += 1

    print()
    print(f"=== 完了: {ok} 件成功 / {err} 件失敗 ===")
    if ok > 0:
        print(f"\n次のステップ: python main.py --sync-vectors  で embedding を付与してください")


if __name__ == "__main__":
    main()
