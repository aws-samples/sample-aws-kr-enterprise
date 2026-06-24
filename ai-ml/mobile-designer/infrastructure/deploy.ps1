#Requires -Version 7.0
<#
.SYNOPSIS
    MDesigner CloudFormation Deployment Script (Windows PowerShell)
.DESCRIPTION
    Full deployment: ECR setup, Docker build/push, Lambda packaging, CFn deploy.
.EXAMPLE
    .\deploy.ps1 -StackName mdesigner -AdminEmail admin@mdesigner.dev -AdminPassword 'YourSecurePassword123'
.EXAMPLE
    .\deploy.ps1 -StackName mdesigner -AdminEmail admin@example.com -AdminPassword Pass123 -SkipBuild -ApiImage "123456.dkr.ecr.ap-northeast-2.amazonaws.com/api:latest" -WebImage "123456.dkr.ecr.ap-northeast-2.amazonaws.com/web:latest"
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$StackName,

    [Parameter(Mandatory = $true)]
    [string]$AdminEmail,

    [Parameter(Mandatory = $true)]
    [string]$AdminPassword,

    [string]$ApiImage = "",
    [string]$WebImage = "",
    [string]$Region = "ap-northeast-2",
    [string]$Environment = "production",
    [string]$TablePrefix = "MDesigner",
    [string]$VpcCidr = "10.0.0.0/16",
    [string]$DomainName = "",
    [string]$TemplatesBucket = "",
    [string]$EcrPrefix = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

# Prevent AWS CLI from opening a pager on Windows
$env:AWS_PAGER = ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

# Set ECR prefix default
if (-not $EcrPrefix) {
    $EcrPrefix = $StackName
}

# ============================================
# Step 0a: Check prerequisites
# ============================================
Write-Host "[0/7] Checking prerequisites..." -ForegroundColor Cyan

$missing = @()
if (-not (Get-Command "aws" -ErrorAction SilentlyContinue)) { $missing += "aws" }
if (-not (Get-Command "docker" -ErrorAction SilentlyContinue)) { $missing += "docker" }

if ($missing.Count -gt 0) {
    Write-Host "ERROR: Missing required tools: $($missing -join ', ')" -ForegroundColor Red
    Write-Host "  - aws: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html"
    Write-Host "  - docker: https://docs.docker.com/get-docker/"
    exit 1
}

# Check Docker daemon
$null = docker info 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker daemon is not running. Please start Docker Desktop." -ForegroundColor Red
    exit 1
}

# Check AWS credentials
try {
    aws sts get-caller-identity --region $Region 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "AWS credentials invalid" }
} catch {
    Write-Host "ERROR: AWS credentials not configured or expired." -ForegroundColor Red
    exit 1
}

Write-Host "  All prerequisites satisfied."

# Get AWS account ID
$AccountId = (aws sts get-caller-identity --query Account --output text --region $Region).Trim()
$EcrRegistry = "${AccountId}.dkr.ecr.${Region}.amazonaws.com"

# ============================================
# Step 0b-0e: Docker build and push (if needed)
# ============================================
$NeedBuild = (-not $SkipBuild) -and ((-not $ApiImage) -or (-not $WebImage))

if ($NeedBuild) {
    $ApiRepo = "${EcrPrefix}-api"
    $WebRepo = "${EcrPrefix}-web"

    # Step 0b: Create ECR repositories
    Write-Host "[1/7] Creating ECR repositories if needed..." -ForegroundColor Cyan
    foreach ($repo in @($ApiRepo, $WebRepo)) {
        $exists = aws ecr describe-repositories --repository-names $repo --region $Region 2>$null
        if ($LASTEXITCODE -ne 0) {
            aws ecr create-repository `
                --repository-name $repo `
                --region $Region `
                --image-scanning-configuration scanOnPush=true `
                --encryption-configuration encryptionType=AES256 | Out-Null
            Write-Host "  Created repository: $repo"
        } else {
            Write-Host "  Repository exists: $repo"
        }
    }

    # Set lifecycle policy
    $lifecyclePolicy = '{"rules":[{"rulePriority":1,"description":"Keep last 10 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":10},"action":{"type":"expire"}}]}'
    foreach ($repo in @($ApiRepo, $WebRepo)) {
        aws ecr put-lifecycle-policy `
            --repository-name $repo `
            --lifecycle-policy-text $lifecyclePolicy `
            --region $Region 2>$null | Out-Null
    }

    # Step 0c: Docker login to ECR
    Write-Host "[2/7] Logging in to ECR..." -ForegroundColor Cyan
    $ecrPassword = aws ecr get-login-password --region $Region
    $ecrPassword | docker login --username AWS --password-stdin $EcrRegistry
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Docker login to ECR failed" -ForegroundColor Red
        exit 1
    }
    Write-Host ""

    # Step 0d: Build Docker images
    Write-Host "[3/7] Building Docker images (--platform linux/amd64)..." -ForegroundColor Cyan
    $ImageTag = Get-Date -Format "yyyyMMdd-HHmmss"

    if (-not $ApiImage) {
        Write-Host "  Building API image..."
        docker build --platform linux/amd64 `
            -t "${EcrRegistry}/${ApiRepo}:${ImageTag}" `
            -t "${EcrRegistry}/${ApiRepo}:latest" `
            "$ProjectRoot/apps/api"
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: API image build failed" -ForegroundColor Red; exit 1 }
        $ApiImage = "${EcrRegistry}/${ApiRepo}:${ImageTag}"
        Write-Host "  API image: $ApiImage"
    }

    if (-not $WebImage) {
        Write-Host "  Building Web image..."
        docker build --platform linux/amd64 `
            -t "${EcrRegistry}/${WebRepo}:${ImageTag}" `
            -t "${EcrRegistry}/${WebRepo}:latest" `
            "$ProjectRoot/apps/web"
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Web image build failed" -ForegroundColor Red; exit 1 }
        $WebImage = "${EcrRegistry}/${WebRepo}:${ImageTag}"
        Write-Host "  Web image: $WebImage"
    }

    # Step 0e: Push images to ECR
    Write-Host "[4/7] Pushing Docker images to ECR..." -ForegroundColor Cyan
    if ($ApiImage -like "${EcrRegistry}/${ApiRepo}:*") {
        docker push "${EcrRegistry}/${ApiRepo}:${ImageTag}"
        docker push "${EcrRegistry}/${ApiRepo}:latest"
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: API image push failed" -ForegroundColor Red; exit 1 }
        Write-Host "  Pushed API image"
    }
    if ($WebImage -like "${EcrRegistry}/${WebRepo}:*") {
        docker push "${EcrRegistry}/${WebRepo}:${ImageTag}"
        docker push "${EcrRegistry}/${WebRepo}:latest"
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Web image push failed" -ForegroundColor Red; exit 1 }
        Write-Host "  Pushed Web image"
    }
} else {
    Write-Host "[1/7] Skipping ECR setup (--SkipBuild or images provided)" -ForegroundColor Yellow
    Write-Host "[2/7] Skipping ECR login" -ForegroundColor Yellow
    Write-Host "[3/7] Skipping Docker build" -ForegroundColor Yellow
    Write-Host "[4/7] Skipping Docker push" -ForegroundColor Yellow

    if (-not $ApiImage) {
        Write-Host "ERROR: -ApiImage is required when using -SkipBuild" -ForegroundColor Red
        exit 1
    }
    if (-not $WebImage) {
        Write-Host "ERROR: -WebImage is required when using -SkipBuild" -ForegroundColor Red
        exit 1
    }
}

# Determine templates bucket
if (-not $TemplatesBucket) {
    $TemplatesBucket = "${StackName}-cfn-templates-${AccountId}"
}
$TemplatesPrefix = "infrastructure"

Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "MDesigner CloudFormation Deployment"
Write-Host "==============================================" -ForegroundColor Green
Write-Host "Stack Name:        $StackName"
Write-Host "Region:            $Region"
Write-Host "Environment:       $Environment"
Write-Host "Templates Bucket:  $TemplatesBucket"
Write-Host "API Image:         $ApiImage"
Write-Host "Web Image:         $WebImage"
Write-Host "Admin Email:       $AdminEmail"
Write-Host "Table Prefix:      $TablePrefix"
Write-Host "VPC CIDR:          $VpcCidr"
Write-Host "Domain:            $(if ($DomainName) { $DomainName } else { 'none' })"
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""

# Step 5: Create templates bucket
Write-Host "[5/7] Ensuring templates S3 bucket exists..." -ForegroundColor Cyan
$bucketExists = aws s3api head-bucket --bucket $TemplatesBucket --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
    aws s3api create-bucket `
        --bucket $TemplatesBucket `
        --region $Region `
        --create-bucket-configuration LocationConstraint=$Region | Out-Null
    Write-Host "  Created bucket: $TemplatesBucket"
} else {
    Write-Host "  Bucket already exists: $TemplatesBucket"
}

# Package bootstrap lambda
Write-Host "[5/7] Packaging bootstrap Lambda..." -ForegroundColor Cyan
$BuildDir = Join-Path ([System.IO.Path]::GetTempPath()) "mdesigner-deploy-$(Get-Date -Format 'yyyyMMddHHmmss')"
New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null

$LambdaZip = Join-Path $BuildDir "lambda.zip"
$LayerZip = Join-Path $BuildDir "bcrypt-layer.zip"

# Package lambda code (use Push-Location to avoid directory prefix in zip)
Copy-Item "$ScriptDir/bootstrap/index.py" "$BuildDir/index.py"
Push-Location $BuildDir
Compress-Archive -Path "index.py" -DestinationPath $LambdaZip -Force
Pop-Location
Write-Host "  Lambda package: $LambdaZip"

# Package bcrypt layer (must be Linux x86_64 for Lambda)
# Use Docker to build layer zip with correct Linux paths
$BuildDirForDocker = $BuildDir -replace '\\', '/'
docker run --rm --platform linux/amd64 -v "${BuildDirForDocker}:/out" python:3.12-slim bash -c "pip install --quiet --target /out/python bcrypt==4.2.1 && cd /out && apt-get update -qq && apt-get install -y -qq zip > /dev/null && zip -qr /out/bcrypt-layer.zip python/"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: bcrypt layer build failed" -ForegroundColor Red
    exit 1
}
Write-Host "  Layer package: $LayerZip"

# Upload lambda artifacts to S3
aws s3 cp $LambdaZip "s3://${TemplatesBucket}/bootstrap/lambda.zip" --region $Region --quiet
aws s3 cp $LayerZip "s3://${TemplatesBucket}/bootstrap/bcrypt-layer.zip" --region $Region --quiet
Write-Host "  Uploaded to s3://${TemplatesBucket}/bootstrap/"

# Cleanup
Remove-Item -Recurse -Force $BuildDir

# Step 6: Upload CloudFormation templates
Write-Host "[6/7] Uploading CloudFormation templates..." -ForegroundColor Cyan
$templates = @("template.yaml", "networking.yaml", "storage.yaml", "security.yaml", "compute.yaml", "bootstrap.yaml", "cdn.yaml")
foreach ($template in $templates) {
    aws s3 cp "$ScriptDir/$template" `
        "s3://${TemplatesBucket}/${TemplatesPrefix}/${template}" `
        --region $Region --quiet
    Write-Host "  Uploaded: $template"
}

# Step 7: Deploy CloudFormation stack
Write-Host "[7/7] Deploying CloudFormation stack..." -ForegroundColor Cyan

$params = @(
    "ParameterKey=Environment,ParameterValue=$Environment",
    "ParameterKey=AdminEmail,ParameterValue=$AdminEmail",
    "ParameterKey=AdminInitialPassword,ParameterValue=$AdminPassword",
    "ParameterKey=ApiImageUri,ParameterValue=$ApiImage",
    "ParameterKey=WebImageUri,ParameterValue=$WebImage",
    "ParameterKey=VpcCidr,ParameterValue=$VpcCidr",
    "ParameterKey=TablePrefix,ParameterValue=$TablePrefix",
    "ParameterKey=TemplatesBucketName,ParameterValue=$TemplatesBucket",
    "ParameterKey=TemplatesPrefix,ParameterValue=$TemplatesPrefix"
)

if ($DomainName) {
    $params += "ParameterKey=DomainName,ParameterValue=$DomainName"
}

# Check if stack exists
$stackStatus = aws cloudformation describe-stacks `
    --stack-name $StackName `
    --region $Region `
    --query 'Stacks[0].StackStatus' `
    --output text 2>$null

if ($LASTEXITCODE -ne 0) {
    $stackStatus = "DOES_NOT_EXIST"
}

if ($stackStatus -eq "DOES_NOT_EXIST") {
    Write-Host "  Creating new stack..."
    aws cloudformation create-stack `
        --stack-name $StackName `
        --template-url "https://${TemplatesBucket}.s3.amazonaws.com/${TemplatesPrefix}/template.yaml" `
        --parameters $params `
        --capabilities CAPABILITY_NAMED_IAM `
        --region $Region `
        --tags Key=Project,Value=MDesigner Key=Environment,Value=$Environment

    Write-Host "  Waiting for stack creation to complete (this may take 15-20 minutes)..."
    aws cloudformation wait stack-create-complete `
        --stack-name $StackName `
        --region $Region
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Stack creation failed" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  Updating existing stack..."
    $updateResult = aws cloudformation update-stack `
        --stack-name $StackName `
        --template-url "https://${TemplatesBucket}.s3.amazonaws.com/${TemplatesPrefix}/template.yaml" `
        --parameters $params `
        --capabilities CAPABILITY_NAMED_IAM `
        --region $Region `
        --tags Key=Project,Value=MDesigner Key=Environment,Value=$Environment 2>&1

    if ($LASTEXITCODE -ne 0) {
        if ($updateResult -match "No updates are to be performed") {
            Write-Host "  No updates to perform (stack is up-to-date)"
        } else {
            Write-Host "  ERROR: Stack update failed" -ForegroundColor Red
            Write-Host $updateResult
            exit 1
        }
    } else {
        Write-Host "  Waiting for stack update to complete..."
        aws cloudformation wait stack-update-complete `
            --stack-name $StackName `
            --region $Region
    }
}

# Display outputs
Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "Deployment Complete!" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
aws cloudformation describe-stacks `
    --stack-name $StackName `
    --region $Region `
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' `
    --output table
Write-Host ""

# Post-deployment: Seed prompts with actual content from source code
Write-Host "[Post-deploy] Seeding prompts to production S3..." -ForegroundColor Cyan
$FilesBucket = (aws cloudformation describe-stacks `
    --stack-name $StackName `
    --region $Region `
    --query 'Stacks[0].Outputs[?OutputKey==`FilesBucketName`].OutputValue' `
    --output text 2>$null).Trim()

if ($FilesBucket) {
    # Determine correct Python path based on platform
    $PythonExe = if ($IsWindows -or $env:OS -eq "Windows_NT") { Join-Path $ProjectRoot "apps/api/.venv/Scripts/python.exe" } else { Join-Path $ProjectRoot "apps/api/.venv/bin/python" }

    # Write seed script to temp file to avoid here-string issues
    $SeedScript = @'
import asyncio, os
from src.common.config import Settings
from src.common.s3.client import S3Client
from src.common.db.client import DynamoDBClient
from src.common.db.tables import PROMPTS_TABLE
from src.ai.orchestrator import (
    CHATBOT_SYSTEM_PROMPT, WIREFRAME_SYSTEM_PROMPT, DESIGNER_SYSTEM_PROMPT,
    MODIFY_SYSTEM_PROMPT, WIREFRAME_CHAT_PROMPT, DESIGN_CHAT_PROMPT,
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
'@
    $SeedScriptPath = Join-Path ([System.IO.Path]::GetTempPath()) "mdesigner-seed-prompts.py"
    $SeedScript | Set-Content -Path $SeedScriptPath -Encoding UTF8

    Push-Location (Join-Path $ProjectRoot "apps/api")
    $env:MDESIGNER_S3_BUCKET_NAME = $FilesBucket
    $env:MDESIGNER_AWS_REGION = $Region
    & $PythonExe $SeedScriptPath
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  Prompts seeded successfully" -ForegroundColor Green
    } else {
        Write-Host "  WARNING: Prompt seeding failed (run manually: cd apps/api && python scripts/seed_prompts.py)" -ForegroundColor Yellow
    }
    # cleanup
    Pop-Location
    Remove-Item $SeedScriptPath -ErrorAction SilentlyContinue
    Remove-Item Env:\MDESIGNER_S3_BUCKET_NAME -ErrorAction SilentlyContinue
    Remove-Item Env:\MDESIGNER_AWS_REGION -ErrorAction SilentlyContinue
} else {
    Write-Host "  WARNING: Could not determine files bucket, skipping prompt seed" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Deployment successful!" -ForegroundColor Green
