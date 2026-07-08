#!/usr/bin/env bash
# Wasabi 管理 WebApp を Cloud Run にデプロイする
# 使い方: bash infra/deploy_wasabi_webapp.sh
# 前提: gcloud 認証済み・Artifact Registry リポジトリ（smart-sync）作成済み
set -euo pipefail

PROJECT_ID="andst-hd-ax"
REGION="asia-northeast1"
SERVICE="wasabi-webapp"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/smart-sync/${SERVICE}:latest"
SA_EMAIL="smart-sync-sa@${PROJECT_ID}.iam.gserviceaccount.com"   # 専用 SA を作る場合は変更

echo "=== build & push ==="
# Dockerfile が webapp/ 配下にあるため cloudbuild 設定経由でビルドする
gcloud builds submit --config infra/cloudbuild_webapp.yaml --project "${PROJECT_ID}" .

echo "=== deploy ==="
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --service-account="${SA_EMAIL}" \
  --set-env-vars="SMARTSYNC_FIRESTORE_DATABASE=smart-sync-stg,DEBUG_MODE=false,BACKLOG_BASE_URL=https://adastria.backlog.jp" \
  --set-secrets="BACKLOG_API_KEY=wasabi-backlog-api-key:latest" \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=2 \
  --no-allow-unauthenticated

echo "=== 完了 ==="
echo "IAP の有効化は GCP Console > Security > Identity-Aware Proxy から実施してください。"
gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT_ID}" \
  --format='value(status.url)'
