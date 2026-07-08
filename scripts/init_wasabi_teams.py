"""
config.yaml の現行設定を wasabi_teams/sales へ初回移行するスクリプト（使い捨て）

使い方:
  python scripts/init_wasabi_teams.py --dry-run   # 内容確認のみ
  python scripts/init_wasabi_teams.py             # 実際に登録
  python scripts/init_wasabi_teams.py --admin ta.kimura@andst-hd.co.jp
      # wasabi_admins に管理者メールも登録
"""
import sys
import os
import json
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import truststore
truststore.inject_into_ssl()

from main import load_config
from src import smartsync_client as sc
from src import team_config


def main():
    parser = argparse.ArgumentParser(description="wasabi_teams 初回移行")
    parser.add_argument("--dry-run", action="store_true", help="内容確認のみ")
    parser.add_argument("--admin", default="", help="wasabi_admins に登録するメールアドレス")
    args = parser.parse_args()

    config = load_config()
    profile = team_config._from_config_yaml(config)

    print(f"=== wasabi_teams/sales 初回移行 {'[DRY-RUN]' if args.dry_run else ''} ===")
    print(f"移行先DB: {sc._database()}")
    print()
    preview = dict(profile)
    preview["channel_mapping"] = f"({len(profile['channel_mapping'])} チャンネル)"
    print(json.dumps(preview, ensure_ascii=False, indent=2, default=str))
    print()
    print(f"channel_mapping 内訳:")
    for ch, m in profile["channel_mapping"].items():
        parent = m.get("parent_issue_key") or "（自動判別）"
        print(f"  #{ch} → {parent}")

    if args.dry_run:
        print("\n--dry-run: 書き込みはスキップしました。")
        return

    # 既存チェック
    existing = team_config.load_team("sales")
    if existing.get("team_name") and existing.get("updated_at"):
        ans = input("\nwasabi_teams/sales は既に存在します。上書きしますか？ [y/N]: ")
        if ans.lower() != "y":
            print("中止しました。")
            return

    team_config.save_team("sales", profile, updated_by="init_script")
    print("\n✅ wasabi_teams/sales を登録しました。")

    if args.admin:
        sc.save_doc("wasabi_admins", args.admin, {
            "email": args.admin,
            "created_at": datetime.now(timezone.utc),
        })
        print(f"✅ wasabi_admins/{args.admin} を登録しました。")


if __name__ == "__main__":
    main()
