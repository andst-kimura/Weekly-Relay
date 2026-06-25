#!/bin/bash
# Weekly Relay — Firestore セットアップスクリプト
#
# 実行前提:
#   - gcloud CLI がインストール済みであること
#   - 個人用 GCP プロジェクト "weekly-relay" のオーナー権限があること
#
# DB 名を "weekly-relay" として Named Database で作成する。
# 将来 andst-hd-ax プロジェクトへ統合する際も smart-sync DB と明確に区別できる。
#
# 実行方法（Git Bash / WSL）:
#   bash infra/setup_firestore.sh
#
set -euo pipefail

PROJECT_ID="weekly-relay"
DB="weekly-relay"
REGION="asia-northeast1"

echo "=== Weekly Relay — Firestore Setup ==="
echo "Project : ${PROJECT_ID}"
echo "Database: ${DB}"
echo "Region  : ${REGION}"
echo ""

# ------------------------------------------------------------------ #
# 1. ADC 認証確認
# ------------------------------------------------------------------ #
echo "--- 1. ADC 認証確認 ---"
if ! gcloud auth application-default print-access-token &>/dev/null; then
  echo "ADC 未設定。ブラウザで認証を行います..."
  gcloud auth application-default login
else
  echo "  ✓ ADC 認証済み"
fi

gcloud config set project "${PROJECT_ID}"

# ------------------------------------------------------------------ #
# 2. Firestore API 有効化
# ------------------------------------------------------------------ #
echo ""
echo "--- 2. Firestore API 有効化 ---"
gcloud services enable firestore.googleapis.com \
  --project="${PROJECT_ID}" \
  && echo "  ✓ firestore.googleapis.com"

# ------------------------------------------------------------------ #
# 3. Firestore Named Database 作成（weekly-relay）
# ------------------------------------------------------------------ #
echo ""
echo "--- 3. Firestore Database 作成: ${DB} ---"
if gcloud firestore databases describe \
    --database="${DB}" \
    --project="${PROJECT_ID}" &>/dev/null; then
  echo "  ✓ DB '${DB}' は既に存在します（スキップ）"
else
  gcloud firestore databases create \
    --database="${DB}" \
    --location="${REGION}" \
    --type=firestore-native \
    --project="${PROJECT_ID}"
  echo "  ✓ DB '${DB}' 作成完了"
fi

# ------------------------------------------------------------------ #
# 4. TTL 設定
#    context_snapshots: expire_at で 30日後に自動削除
#    sync_logs        : expire_at で 90日後に自動削除
# ------------------------------------------------------------------ #
echo ""
echo "--- 4. TTL 設定 ---"

echo "  context_snapshots (TTL=30d) ..."
gcloud firestore fields ttls update expire_at \
  --collection-group=context_snapshots \
  --enable-ttl \
  --database="${DB}" \
  --project="${PROJECT_ID}"
echo "  ✓ context_snapshots TTL 設定完了"

echo "  sync_logs (TTL=90d) ..."
gcloud firestore fields ttls update expire_at \
  --collection-group=sync_logs \
  --enable-ttl \
  --database="${DB}" \
  --project="${PROJECT_ID}"
echo "  ✓ sync_logs TTL 設定完了"

# ------------------------------------------------------------------ #
# 5. 完了メッセージ
# ------------------------------------------------------------------ #
echo ""
echo "=== Setup Complete ==="
echo ""
echo "次のステップ:"
echo "  1. .env に以下を追記する:"
echo "       GOOGLE_CLOUD_PROJECT=weekly-relay"
echo "       FIRESTORE_DATABASE=weekly-relay"
echo ""
echo "  2. 動作確認（ドライラン）:"
echo "       python main.py --run-now --dry-run"
echo ""
echo "確認コマンド:"
echo "  gcloud firestore databases describe --database=${DB} --project=${PROJECT_ID}"
echo "  gcloud firestore fields ttls list --collection-group=context_snapshots --database=${DB} --project=${PROJECT_ID}"
