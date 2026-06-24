#!/bin/bash
set -euo pipefail

# MDesigner CloudFormation Deployment Script
# Full deployment: ECR setup, Docker build/push, Lambda packaging, CFn deploy.
#
# Usage:
#   ./deploy.sh \
#     --stack-name mdesigner \
#     --admin-email admin@mdesigner.dev \
#     --admin-password 'YourSecurePassword123'
#
# Optional:
#   --api-image URI                 Pre-built API image URI (skip build)
#   --web-image URI                 Pre-built Web image URI (skip build)
#   --skip-build                    Skip Docker build/push entirely
#   --ecr-prefix PREFIX             ECR repository prefix (default: stack-name)
#   --region ap-northeast-2         AWS region (default: ap-northeast-2)
#   --environment production        Environment (default: production)
#   --table-prefix MDesigner        DynamoDB table prefix (default: MDesigner)
#   --domain example.com            Custom domain name
#   --vpc-cidr 10.0.0.0/16          VPC CIDR (default: 10.0.0.0/16)
#   --templates-bucket BUCKET       S3 bucket for templates (default: auto-created)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Default values
REGION="ap-northeast-2"
ENVIRONMENT="production"
TABLE_PREFIX="MDesigner"
VPC_CIDR="10.0.0.0/16"
DOMAIN_NAME=""
TEMPLATES_BUCKET=""
STACK_NAME=""
API_IMAGE=""
WEB_IMAGE=""
ADMIN_EMAIL=""
ADMIN_PASSWORD=""
SKIP_BUILD="false"
ECR_PREFIX=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --stack-name) STACK_NAME="$2"; shift 2 ;;
        --api-image) API_IMAGE="$2"; shift 2 ;;
        --web-image) WEB_IMAGE="$2"; shift 2 ;;
        --admin-email) ADMIN_EMAIL="$2"; shift 2 ;;
        --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --environment) ENVIRONMENT="$2"; shift 2 ;;
        --table-prefix) TABLE_PREFIX="$2"; shift 2 ;;
        --domain) DOMAIN_NAME="$2"; shift 2 ;;
        --vpc-cidr) VPC_CIDR="$2"; shift 2 ;;
        --templates-bucket) TEMPLATES_BUCKET="$2"; shift 2 ;;
        --skip-build) SKIP_BUILD="true"; shift ;;
        --ecr-prefix) ECR_PREFIX="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Validate required parameters
if [[ -z "$STACK_NAME" ]]; then
    echo "ERROR: --stack-name is required"
    exit 1
fi
if [[ -z "$ADMIN_EMAIL" ]]; then
    echo "ERROR: --admin-email is required"
    exit 1
fi
if [[ -z "$ADMIN_PASSWORD" ]]; then
    echo "ERROR: --admin-password is required"
    exit 1
fi

# Set ECR prefix (default: stack-name)
if [[ -z "$ECR_PREFIX" ]]; then
    ECR_PREFIX="$STACK_NAME"
fi

# ============================================
# Step 0a: Check prerequisites
# ============================================
echo "[0/7] Checking prerequisites..."

MISSING=()
command -v aws >/dev/null 2>&1 || MISSING+=("aws")
command -v docker >/dev/null 2>&1 || MISSING+=("docker")
command -v zip >/dev/null 2>&1 || MISSING+=("zip")

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "ERROR: Missing required tools: ${MISSING[*]}"
    echo "  - aws: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html"
    echo "  - docker: https://docs.docker.com/get-docker/"
    echo "  - zip: Install via your package manager"
    exit 1
fi

# Check Docker daemon is running
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not running. Please start Docker."
    exit 1
fi

# Check AWS credentials
if ! aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1; then
    echo "ERROR: AWS credentials not configured or expired."
    exit 1
fi

echo "  All prerequisites satisfied."

# Get AWS account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# ============================================
# Step 0b-0e: Docker build and push (if needed)
# ============================================
NEED_BUILD="false"
if [[ "$SKIP_BUILD" == "false" && (-z "$API_IMAGE" || -z "$WEB_IMAGE") ]]; then
    NEED_BUILD="true"
fi

if [[ "$NEED_BUILD" == "true" ]]; then
    API_REPO="${ECR_PREFIX}-api"
    WEB_REPO="${ECR_PREFIX}-web"

    # Step 0b: Create ECR repositories if they don't exist
    echo "[1/7] Creating ECR repositories if needed..."
    for REPO in "$API_REPO" "$WEB_REPO"; do
        if ! aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1; then
            aws ecr create-repository \
                --repository-name "$REPO" \
                --region "$REGION" \
                --image-scanning-configuration scanOnPush=true \
                --encryption-configuration encryptionType=AES256 >/dev/null
            echo "  Created repository: $REPO"
        else
            echo "  Repository exists: $REPO"
        fi
    done

    # Set lifecycle policy to keep only last 10 images
    LIFECYCLE_POLICY='{"rules":[{"rulePriority":1,"description":"Keep last 10 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":10},"action":{"type":"expire"}}]}'
    for REPO in "$API_REPO" "$WEB_REPO"; do
        aws ecr put-lifecycle-policy \
            --repository-name "$REPO" \
            --lifecycle-policy-text "$LIFECYCLE_POLICY" \
            --region "$REGION" >/dev/null 2>&1 || true
    done

    # Step 0c: Docker login to ECR
    echo "[2/7] Logging in to ECR..."
    aws ecr get-login-password --region "$REGION" | \
        docker login --username AWS --password-stdin "$ECR_REGISTRY"
    echo ""

    # Step 0d: Build Docker images
    echo "[3/7] Building Docker images (--platform linux/amd64)..."
    IMAGE_TAG="$(date +%Y%m%d-%H%M%S)"

    if [[ -z "$API_IMAGE" ]]; then
        echo "  Building API image..."
        docker build --platform linux/amd64 \
            -t "${ECR_REGISTRY}/${API_REPO}:${IMAGE_TAG}" \
            -t "${ECR_REGISTRY}/${API_REPO}:latest" \
            "$PROJECT_ROOT/apps/api"
        API_IMAGE="${ECR_REGISTRY}/${API_REPO}:${IMAGE_TAG}"
        echo "  API image: $API_IMAGE"
    fi

    if [[ -z "$WEB_IMAGE" ]]; then
        echo "  Building Web image..."
        docker build --platform linux/amd64 \
            -t "${ECR_REGISTRY}/${WEB_REPO}:${IMAGE_TAG}" \
            -t "${ECR_REGISTRY}/${WEB_REPO}:latest" \
            "$PROJECT_ROOT/apps/web"
        WEB_IMAGE="${ECR_REGISTRY}/${WEB_REPO}:${IMAGE_TAG}"
        echo "  Web image: $WEB_IMAGE"
    fi

    # Step 0e: Push images to ECR
    echo "[4/7] Pushing Docker images to ECR..."
    if [[ "$API_IMAGE" == "${ECR_REGISTRY}/${API_REPO}:"* ]]; then
        docker push "${ECR_REGISTRY}/${API_REPO}:${IMAGE_TAG}"
        docker push "${ECR_REGISTRY}/${API_REPO}:latest"
        echo "  Pushed API image"
    fi
    if [[ "$WEB_IMAGE" == "${ECR_REGISTRY}/${WEB_REPO}:"* ]]; then
        docker push "${ECR_REGISTRY}/${WEB_REPO}:${IMAGE_TAG}"
        docker push "${ECR_REGISTRY}/${WEB_REPO}:latest"
        echo "  Pushed Web image"
    fi
else
    echo "[1/7] Skipping ECR setup (--skip-build or images provided)"
    echo "[2/7] Skipping ECR login"
    echo "[3/7] Skipping Docker build"
    echo "[4/7] Skipping Docker push"

    # Validate images are provided when skipping build
    if [[ -z "$API_IMAGE" ]]; then
        echo "ERROR: --api-image is required when using --skip-build"
        exit 1
    fi
    if [[ -z "$WEB_IMAGE" ]]; then
        echo "ERROR: --web-image is required when using --skip-build"
        exit 1
    fi
fi

# Determine templates bucket
if [[ -z "$TEMPLATES_BUCKET" ]]; then
    TEMPLATES_BUCKET="${STACK_NAME}-cfn-templates-${ACCOUNT_ID}"
fi
TEMPLATES_PREFIX="infrastructure"

echo ""
echo "=============================================="
echo "MDesigner CloudFormation Deployment"
echo "=============================================="
echo "Stack Name:        $STACK_NAME"
echo "Region:            $REGION"
echo "Environment:       $ENVIRONMENT"
echo "Templates Bucket:  $TEMPLATES_BUCKET"
echo "API Image:         $API_IMAGE"
echo "Web Image:         $WEB_IMAGE"
echo "Admin Email:       $ADMIN_EMAIL"
echo "Table Prefix:      $TABLE_PREFIX"
echo "VPC CIDR:          $VPC_CIDR"
echo "Domain:            ${DOMAIN_NAME:-none}"
echo "=============================================="
echo ""

# Step 5: Create templates bucket if it doesn't exist
echo "[5/7] Ensuring templates S3 bucket exists..."
if ! aws s3api head-bucket --bucket "$TEMPLATES_BUCKET" --region "$REGION" 2>/dev/null; then
    aws s3api create-bucket \
        --bucket "$TEMPLATES_BUCKET" \
        --region "$REGION" \
        --create-bucket-configuration LocationConstraint="$REGION"
    echo "  Created bucket: $TEMPLATES_BUCKET"
else
    echo "  Bucket already exists: $TEMPLATES_BUCKET"
fi

# Step 6: Package bootstrap lambda
echo "[5/7] Packaging bootstrap Lambda..."
BUILD_DIR=$(mktemp -d)
LAMBDA_ZIP="$BUILD_DIR/lambda.zip"
LAYER_ZIP="$BUILD_DIR/bcrypt-layer.zip"

# Package lambda code
cp "$SCRIPT_DIR/bootstrap/index.py" "$BUILD_DIR/index.py"
(cd "$BUILD_DIR" && zip -q "$LAMBDA_ZIP" index.py)
echo "  Lambda package: $LAMBDA_ZIP"

# Package bcrypt layer (must be Linux x86_64 for Lambda)
LAYER_DIR="$BUILD_DIR/python"
mkdir -p "$LAYER_DIR"
docker run --rm --platform linux/amd64 -v "$BUILD_DIR:/out" python:3.12-slim \
  pip install --quiet --target /out/python bcrypt==4.2.1 2>/dev/null
(cd "$BUILD_DIR" && zip -qr "$LAYER_ZIP" python/)
echo "  Layer package: $LAYER_ZIP"

# Upload lambda artifacts to S3
aws s3 cp "$LAMBDA_ZIP" "s3://${TEMPLATES_BUCKET}/bootstrap/lambda.zip" --region "$REGION" --quiet
aws s3 cp "$LAYER_ZIP" "s3://${TEMPLATES_BUCKET}/bootstrap/bcrypt-layer.zip" --region "$REGION" --quiet
echo "  Uploaded to s3://${TEMPLATES_BUCKET}/bootstrap/"

# Cleanup
rm -rf "$BUILD_DIR"

# Step 6: Upload CloudFormation templates (root + nested)
echo "[6/7] Uploading CloudFormation templates..."
for template in template.yaml networking.yaml storage.yaml security.yaml compute.yaml bootstrap.yaml cdn.yaml; do
    aws s3 cp "$SCRIPT_DIR/$template" \
        "s3://${TEMPLATES_BUCKET}/${TEMPLATES_PREFIX}/${template}" \
        --region "$REGION" --quiet
    echo "  Uploaded: $template"
done

# Step 7: Deploy CloudFormation stack
echo "[7/7] Deploying CloudFormation stack..."
PARAMS=(
    "ParameterKey=Environment,ParameterValue=$ENVIRONMENT"
    "ParameterKey=AdminEmail,ParameterValue=$ADMIN_EMAIL"
    "ParameterKey=AdminInitialPassword,ParameterValue=$ADMIN_PASSWORD"
    "ParameterKey=ApiImageUri,ParameterValue=$API_IMAGE"
    "ParameterKey=WebImageUri,ParameterValue=$WEB_IMAGE"
    "ParameterKey=VpcCidr,ParameterValue=$VPC_CIDR"
    "ParameterKey=TablePrefix,ParameterValue=$TABLE_PREFIX"
    "ParameterKey=TemplatesBucketName,ParameterValue=$TEMPLATES_BUCKET"
    "ParameterKey=TemplatesPrefix,ParameterValue=$TEMPLATES_PREFIX"
)

if [[ -n "$DOMAIN_NAME" ]]; then
    PARAMS+=("ParameterKey=DomainName,ParameterValue=$DOMAIN_NAME")
fi

# Check if stack exists
STACK_EXISTS=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [[ "$STACK_EXISTS" == "DOES_NOT_EXIST" ]]; then
    echo "  Creating new stack..."
    aws cloudformation create-stack \
        --stack-name "$STACK_NAME" \
        --template-url "https://${TEMPLATES_BUCKET}.s3.amazonaws.com/${TEMPLATES_PREFIX}/template.yaml" \
        --parameters "${PARAMS[@]}" \
        --capabilities CAPABILITY_NAMED_IAM \
        --region "$REGION" \
        --tags Key=Project,Value=MDesigner Key=Environment,Value="$ENVIRONMENT"

    echo "  Waiting for stack creation to complete (this may take 15-20 minutes)..."
    aws cloudformation wait stack-create-complete \
        --stack-name "$STACK_NAME" \
        --region "$REGION"
else
    echo "  Updating existing stack..."
    aws cloudformation update-stack \
        --stack-name "$STACK_NAME" \
        --template-url "https://${TEMPLATES_BUCKET}.s3.amazonaws.com/${TEMPLATES_PREFIX}/template.yaml" \
        --parameters "${PARAMS[@]}" \
        --capabilities CAPABILITY_NAMED_IAM \
        --region "$REGION" \
        --tags Key=Project,Value=MDesigner Key=Environment,Value="$ENVIRONMENT" \
        2>/dev/null || {
            if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
                --query 'Stacks[0].StackStatus' --output text | grep -q "COMPLETE"; then
                echo "  No updates to perform (stack is up-to-date)"
            else
                echo "  ERROR: Stack update failed"
                exit 1
            fi
        }

    echo "  Waiting for stack update to complete..."
    aws cloudformation wait stack-update-complete \
        --stack-name "$STACK_NAME" \
        --region "$REGION" 2>/dev/null || true
fi

# Display outputs
echo ""
echo "=============================================="
echo "Deployment Complete!"
echo "=============================================="
aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
    --output table
echo ""

# Post-deployment: Seed prompts with actual content from source code
echo "[Post-deploy] Seeding prompts to production S3..."
FILES_BUCKET=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`FilesBucketName`].OutputValue' \
    --output text 2>/dev/null)

if [[ -n "$FILES_BUCKET" ]]; then
    cd "$SCRIPT_DIR/../apps/api"
    MDESIGNER_S3_BUCKET_NAME="$FILES_BUCKET" \
    MDESIGNER_AWS_REGION="$REGION" \
    .venv/bin/python -c "
import asyncio, os
os.environ.setdefault('MDESIGNER_S3_BUCKET_NAME', '$FILES_BUCKET')
os.environ.setdefault('MDESIGNER_AWS_REGION', '$REGION')
from src.common.config import Settings
from src.common.s3.client import S3Client
from src.common.db.client import DynamoDBClient
from src.common.db.tables import PROMPTS_TABLE
from src.ai.orchestrator import (
    CHATBOT_SYSTEM_PROMPT, WIREFRAME_SYSTEM_PROMPT, DESIGNER_SYSTEM_PROMPT,
    MODIFY_SYSTEM_PROMPT, WIREFRAME_CHAT_PROMPT, DESIGN_CHAT_PROMPT,
    REQUIREMENTS_SYNTHESIS_PROMPT,
)
from src.handoff.code_generator.llm_code_generator import SCREEN_CODEGEN_PROMPT

settings = Settings()
db = DynamoDBClient(settings)
s3 = S3Client(settings)

PROMPTS = {
    'CHATBOT_SYSTEM': CHATBOT_SYSTEM_PROMPT,
    'WIREFRAME_SYSTEM': WIREFRAME_SYSTEM_PROMPT,
    'DESIGNER_SYSTEM': DESIGNER_SYSTEM_PROMPT,
    'MODIFY_SYSTEM': MODIFY_SYSTEM_PROMPT,
    'WIREFRAME_CHAT': WIREFRAME_CHAT_PROMPT,
    'DESIGN_CHAT': DESIGN_CHAT_PROMPT,
    'SCREEN_CODEGEN': SCREEN_CODEGEN_PROMPT,
    'REQUIREMENTS_SYNTHESIS': REQUIREMENTS_SYNTHESIS_PROMPT,
}

async def main():
    for slot, content in PROMPTS.items():
        result = await db.query(table_name=PROMPTS_TABLE, key_condition_expression='promptSlot = :slot', expression_values={':slot': slot})
        for item in result.get('Items', []):
            if item.get('isActive'):
                await s3.put_object(item['contentKey'], content.encode(), 'text/plain')
                print(f'  Seeded {slot} ({len(content)} chars)')
                break

asyncio.run(main())
" 2>/dev/null && echo "  Prompts seeded successfully" || echo "  WARNING: Prompt seeding failed (run manually: cd apps/api && python scripts/seed_prompts.py)"
    cd "$SCRIPT_DIR"
fi

echo ""
echo "Deployment successful!"
