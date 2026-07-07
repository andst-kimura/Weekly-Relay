"""
weekly-relay Firestore の manual_memos を SmartSync（wasabi_manual_memos）へ移行する
使い捨てスクリプト。

未転記の手動メモが週次転記（backlog_poster.get_manual_memos）で参照されるため、
weekly-relay 廃止前に全件コピーする。

使い方:
  python scripts/migrate_misc_to_smartsync.py --dry-run   # 件数確認のみ
  python scripts/migrate_misc_to_smartsync.py             # 実際に移行
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import truststore
truststore.inject_into_ssl()

import src.smartsync_client as sc


def _fetch_weekly_relay_docs(collection: str) -> list[tuple[str, dict]]:
    """weekly-relay Firestore からコレクション全件を取得する"""
    from src.firestore_client import _col_path, _get_session, _doc_to_dict
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "weekly-relay")
    database = os.environ.get("FIRESTORE_DATABASE", "weekly-relay")
    url = _col_path(project, database, collection)
    results: list[tuple[str, dict]] = []
    params: dict = {"pageSize": 200}
    session = _get_session()
    while True:
        resp = session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        for doc in body.get("documents", []):
            doc_id = doc["name"].split("/")[-1]
            data = _doc_to_dict(doc)
            if data:
                results.append((doc_id, data))
        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return results


def main():
    parser = argparse.ArgumentParser(description="manual_memos を SmartSync へ移行")
    parser.add_argument("--dry-run", action="store_true", help="件数確認のみ（書き込みなし）")
    args = parser.parse_args()

    print(f"=== manual_memos 移行 {'[DRY-RUN]' if args.dry_run else ''} ===")
    print(f"移行先DB: {sc._database()}")
    print()

    docs = _fetch_weekly_relay_docs("manual_memos")
    print(f"weekly-relay manual_memos: {len(docs)} 件")

    if args.dry_run:
        for doc_id, data in docs:
            print(f"  {doc_id}: {(data.get('text') or '')[:40]}")
        print("\n--dry-run: 書き込みはスキップしました。")
        return

    ok = 0
    err = 0
    for i, (doc_id, data) in enumerate(docs, 1):
        try:
            sc.save_doc("wasabi_manual_memos", doc_id, data)
            print(f"  [{i}/{len(docs)}] OK: {doc_id}")
            ok += 1
        except Exception as e:
            print(f"  [{i}/{len(docs)}] NG: {doc_id} → {e}")
            err += 1

    print()
    print(f"=== 完了: {ok} 件成功 / {err} 件失敗 ===")


if __name__ == "__main__":
    main()
