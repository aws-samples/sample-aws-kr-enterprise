# Base image pulled from Amazon ECR Public (Docker Official mirror).
# Multi-arch manifest (amd64 + arm64/v8) — CodeBuild selects the host arch (amd64 for ECS Fargate).
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV AWS_REGION="${AWS_REGION:-ap-northeast-2}" \
    DYNAMODB_TABLE="${DYNAMODB_TABLE}"
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
