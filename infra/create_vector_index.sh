#!/usr/bin/env bash
# Firestore ベクトルインデックス作成スクリプト
#
# 使い方:
#   bash infra/create_vector_index.sh
#
# 前提:
#   - gcloud CLI がインストール済みで認証済みであること
#   - PROJECT_ID 変数を環境変数またはスクリプト内で設定すること
#
# このスクリプトは context_snapshots コレクションの embedding フィールドに
# COSINE 距離のベクトルインデックスを作成する。
# インデックスが存在しないと vector_search() が 400 エラーを返す。

set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "エラー: 環境変数 GOOGLE_CLOUD_PROJECT を設定してください"
  echo "  例: export GOOGLE_CLOUD_PROJECT=your-project-id"
  exit 1
fi

COLLECTION="context_snapshots"
FIELD="embedding"
DIMENSION=768       # gemini embedding-001 の次元数
DISTANCE="COSINE"   # COSINE / DOT_PRODUCT / EUCLIDEAN

echo "===== Firestore ベクトルインデックス作成 ====="
echo "  プロジェクト : $PROJECT_ID"
echo "  コレクション : $COLLECTION"
echo "  フィールド   : $FIELD (${DIMENSION}次元, $DISTANCE)"

gcloud firestore indexes composite create \
  --project="$PROJECT_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config="field-path=${FIELD},vector-config={\"dimension\":${DIMENSION},\"flat\":{}}" 2>&1

echo ""
echo "インデックス作成リクエストを送信しました。"
echo "反映には数分かかる場合があります。"
echo "以下のコマンドで状態を確認できます:"
echo "  gcloud firestore indexes composite list --project=$PROJECT_ID"
