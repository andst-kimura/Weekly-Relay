"""
SmartSync stg → 本番への wasabi_* データコピー（Phase 1 移行用・使い捨て）

コピー対象:
  - context_snapshots の wasabi_* ドキュメント（embedding は除外・移行後に再生成）
  - wasabi_teams / wasabi_admins / wasabi_manual_memos / wasabi_job_status

使い方:
  python scripts/copy_stg_to_prod.py --dry-run   # 件数確認のみ
  python scripts/copy_stg_to_prod.py             # 実際にコピー
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

SRC_DB = "smart-sync-stg"
DST_DB = "smart-sync"

# コピー対象コレクション（context_snapshots は wasabi_* のみ）
COLLECTIONS = ["wasabi_teams", "wasabi_admins", "wasabi_manual_memos", "wasabi_job_status"]


def main():
    parser = argparse.ArgumentParser(description="stg → 本番 wasabi_* コピー")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from src import smartsync_client as sc

    def with_db(db, fn, *fn_args, **fn_kwargs):
        """SMARTSYNC_FIRESTORE_DATABASE を一時的に切り替えて実行"""
        prev = os.environ.get("SMARTSYNC_FIRESTORE_DATABASE", "")
        os.environ["SMARTSYNC_FIRESTORE_DATABASE"] = db
        try:
            return fn(*fn_args, **fn_kwargs)
        finally:
            os.environ["SMARTSYNC_FIRESTORE_DATABASE"] = prev

    print(f"=== stg → 本番 コピー {'[DRY-RUN]' if args.dry_run else ''} ===")
    print(f"src: {SRC_DB} → dst: {DST_DB}")
    print()

    total_ok = 0
    total_err = 0

    # ① wasabi_* コレクション
    for col in COLLECTIONS:
        docs = with_db(SRC_DB, sc.list_docs, col)
        print(f"{col}: {len(docs)} 件")
        if args.dry_run:
            continue
        for doc_id, data in docs:
            try:
                with_db(DST_DB, sc.save_doc, col, doc_id, data)
                total_ok += 1
            except Exception as e:
                print(f"  NG {col}/{doc_id}: {e}")
                total_err += 1

    # ② context_snapshots（wasabi_* のみ・embedding 除外）
    all_docs = with_db(SRC_DB, sc.list_context_snapshots)
    wasabi_docs = [(d, data) for d, data in all_docs if d.startswith("wasabi_")]
    print(f"context_snapshots (wasabi_*): {len(wasabi_docs)} 件")
    if not args.dry_run:
        for i, (doc_id, data) in enumerate(wasabi_docs, 1):
            try:
                payload = {k: v for k, v in data.items() if k != "embedding"}
                with_db(DST_DB, sc.save_doc, "context_snapshots", doc_id, payload)
                total_ok += 1
                if i % 50 == 0:
                    print(f"  ... {i}/{len(wasabi_docs)}")
            except Exception as e:
                print(f"  NG {doc_id}: {e}")
                total_err += 1

    if args.dry_run:
        print("\n--dry-run: 書き込みはスキップしました。")
    else:
        print(f"\n=== 完了: {total_ok} 件成功 / {total_err} 件失敗 ===")
        print("次のステップ: .env を smart-sync に切替後 `python main.py --sync-vectors`")


if __name__ == "__main__":
    main()
