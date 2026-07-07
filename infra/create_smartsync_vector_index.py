"""
SmartSync Firestore に Wasabi 用 Vector Index を作成するスクリプト。

使い方:
    python infra/create_smartsync_vector_index.py [--db smart-sync-stg|smart-sync]

gcloud 不要。サービスアカウントキーと REST API で直接作成。
"""
import argparse
import json
import os
import sys
import time

import truststore
truststore.inject_into_ssl()

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

import google.oauth2.service_account
import google.auth.transport.requests

_PROJECT = "andst-hd-ax"
_SA_KEY  = os.environ.get(
    "SMARTSYNC_GOOGLE_APPLICATION_CREDENTIALS",
    r"config/andst-hd-ax-276f47e40899.json",
)


def get_session(sa_key: str):
    creds = google.oauth2.service_account.Credentials.from_service_account_file(
        sa_key,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    session = google.auth.transport.requests.AuthorizedSession(creds)
    session.verify = False
    return session


def create_vector_index(database: str, session) -> None:
    """context_snapshots.embedding に Vector Index を作成する。"""
    url = (
        f"https://firestore.googleapis.com/v1/projects/{_PROJECT}"
        f"/databases/{database}/collectionGroups/context_snapshots/indexes"
    )
    body = {
        "queryScope": "COLLECTION",
        "fields": [
            {
                "fieldPath": "embedding",
                "vectorConfig": {
                    "dimension": 768,
                    "flat": {}
                }
            }
        ]
    }
    print(f"[INFO] Vector Index 作成リクエスト送信: {database}/context_snapshots.embedding (768次元)")
    resp = session.post(url, json=body, timeout=30)

    if resp.status_code == 409:
        print("[INFO] すでに同じ Vector Index が存在します（スキップ）")
        return
    if resp.status_code not in (200, 201):
        print(f"[ERROR] 作成失敗: {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)

    op = resp.json()
    op_name = op.get("name", "")
    print(f"[INFO] 作成開始（非同期）: {op_name}")
    print("[INFO] インデックス作成中（数分かかります）... ポーリング開始")

    # 進捗ポーリング
    op_url = f"https://firestore.googleapis.com/v1/{op_name}"
    for i in range(60):
        time.sleep(10)
        poll = session.get(op_url, timeout=30).json()
        state = poll.get("metadata", {}).get("state", "UNKNOWN")
        print(f"  [{i*10}秒] state={state}")
        if poll.get("done"):
            if "error" in poll:
                print(f"[ERROR] インデックス作成エラー: {poll['error']}")
                sys.exit(1)
            print("[INFO] ✅ Vector Index 作成完了！")
            return

    print("[WARN] タイムアウト。Firebase Console で作成状況を確認してください。")


def list_vector_indexes(database: str, session) -> None:
    """既存の Vector Index を一覧表示する。"""
    url = (
        f"https://firestore.googleapis.com/v1/projects/{_PROJECT}"
        f"/databases/{database}/collectionGroups/context_snapshots/indexes"
    )
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    indexes = resp.json().get("indexes", [])
    if not indexes:
        print("[INFO] インデックスなし")
        return
    for idx in indexes:
        fields = idx.get("fields", [])
        state  = idx.get("state", "")
        print(f"  - {idx.get('name', '').split('/')[-1]}  state={state}")
        for f in fields:
            vc = f.get("vectorConfig")
            if vc:
                print(f"    フィールド: {f['fieldPath']}  次元: {vc.get('dimension')}  state={state}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="smart-sync-stg",
                        help="Firestore データベース名（default: smart-sync-stg）")
    parser.add_argument("--list", action="store_true",
                        help="既存インデックス一覧を表示して終了")
    args = parser.parse_args()

    session = get_session(_SA_KEY)

    if args.list:
        print(f"[INFO] 既存インデックス一覧: {args.db}")
        list_vector_indexes(args.db, session)
    else:
        create_vector_index(args.db, session)
