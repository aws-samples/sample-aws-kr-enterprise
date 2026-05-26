#!/bin/bash
# scripts/build-images.sh — Build container images via CodeBuild
# Packages source, uploads to S3, triggers CodeBuild, waits for completion.
set -euo pipefail

: "${AWS_REGION:?Set AWS_REGION}"
: "${ACCOUNT_ID:?Set ACCOUNT_ID}"
: "${PROJECT_PREFIX:=aiops-v2-dev}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Resolve CodeBuild resource names from Terraform outputs or defaults
CB_PROJECT_X86="${CB_PROJECT_X86:-${PROJECT_PREFIX}-build-x86}"
CB_PROJECT_ARM64="${CB_PROJECT_ARM64:-${PROJECT_PREFIX}-build-arm64}"
S3_BUCKET="${CB_SOURCE_BUCKET:-${PROJECT_PREFIX}-codebuild-source-${ACCOUNT_ID}-${AWS_REGION}}"

echo "=== Building Container Images via CodeBuild ==="
echo "  x86 project:   $CB_PROJECT_X86"
echo "  arm64 project:  $CB_PROJECT_ARM64"
echo "  S3 bucket:      $S3_BUCKET"
echo ""

# Package source
echo "▶ Packaging source..."
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

cd "$PROJECT_ROOT"
zip -qr "$TMPDIR/source-x86.zip" code/control-plane/ code/buildspec-x86.yml
zip -qr "$TMPDIR/source-arm64.zip" code/agent-runtime/ code/buildspec-arm64.yml

# Upload to S3
echo "▶ Uploading source to S3..."
aws s3 cp "$TMPDIR/source-x86.zip" "s3://${S3_BUCKET}/source-x86.zip" --region "$AWS_REGION"
aws s3 cp "$TMPDIR/source-arm64.zip" "s3://${S3_BUCKET}/source-arm64.zip" --region "$AWS_REGION"

# Trigger builds in parallel
echo "▶ Starting CodeBuild projects..."
BUILD_ID_X86=$(aws codebuild start-build \
  --project-name "$CB_PROJECT_X86" \
  --region "$AWS_REGION" \
  --query 'build.id' --output text)
echo "  x86 build: $BUILD_ID_X86"

BUILD_ID_ARM64=$(aws codebuild start-build \
  --project-name "$CB_PROJECT_ARM64" \
  --region "$AWS_REGION" \
  --query 'build.id' --output text)
echo "  arm64 build: $BUILD_ID_ARM64"

# Wait for both builds
echo "▶ Waiting for builds to complete..."
wait_for_build() {
  local build_id="$1"
  local label="$2"
  while true; do
    STATUS=$(aws codebuild batch-get-builds --ids "$build_id" --region "$AWS_REGION" \
      --query 'builds[0].buildStatus' --output text)
    case "$STATUS" in
      SUCCEEDED)
        echo "  ✓ $label: SUCCEEDED"
        return 0
        ;;
      FAILED|FAULT|TIMED_OUT|STOPPED)
        echo "  ✗ $label: $STATUS"
        echo "    View logs: https://${AWS_REGION}.console.aws.amazon.com/codesuite/codebuild/projects/${build_id%%:*}/build/${build_id}/log"
        return 1
        ;;
      *)
        echo -n "."
        sleep 15
        ;;
    esac
  done
}

FAIL=0
wait_for_build "$BUILD_ID_X86" "x86 (platform-api, frontend)" || FAIL=1
wait_for_build "$BUILD_ID_ARM64" "arm64 (base-image, report-image)" || FAIL=1

if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo "ERROR: One or more builds failed. Check CloudWatch Logs for details."
  exit 1
fi

echo ""
echo "=== All images built and pushed to ECR ==="
