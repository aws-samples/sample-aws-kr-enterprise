"""
AWS IaC MCP Lambda - CloudFormation/CDK validation, troubleshooting, documentation
CloudFormation/CDK 검증, 배포 문제 해결, 문서 검색을 위한 AWS IaC MCP 람다
"""
import json
import socket
import ipaddress
import urllib.request
import urllib.parse
import re
from html.parser import HTMLParser
from cross_account import get_client, get_role_arn, default_region


def _assert_public_http_url(url):
    """SSRF guard for the doc-page reader.

    Validates the scheme is http(s), then resolves the hostname and rejects the
    request if ANY resolved address is private/loopback/link-local/reserved (e.g.
    169.254.169.254, 127.0.0.1, 10./172.16./192.168., ::1). Prevents the Lambda
    from being coerced into fetching internal endpoints or instance metadata.
    스킴 검증 후 호스트를 해석하여 사설/루프백/링크로컬/예약 주소면 차단 (SSRF 방지)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http/https URLs are supported")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")

    # Resolve all A/AAAA records; block if any address is non-public. / 모든 주소 해석 후 비공개 주소 차단.
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed: {e}")

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise ValueError("Unresolvable host address")
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError("Blocked non-public destination address")


class _TextExtractor(HTMLParser):
    """Extract visible text from HTML, skipping <script>/<style> content.

    Uses the stdlib HTML parser instead of regex tag stripping so that
    script/style bodies are dropped by a real parser (regex-based tag
    filtering is bypassable). / 정규식 대신 표준 HTML 파서로 스크립트/스타일
    본문을 제거하고 보이는 텍스트만 추출한다."""

    _SKIP = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self):
        return "".join(self._parts)


# Static content tools / 정적 콘텐츠 도구

CDK_BEST_PRACTICES = """# CDK Best Practices
## Security
- Use IAM least-privilege policies, avoid wildcards
- Enable encryption at rest (KMS) and in transit (TLS)
- Use VPC with private subnets for compute resources
- Enable CloudTrail and VPC Flow Logs

## Architecture
- One stack per environment (dev/staging/prod)
- Use constructs for reusable components
- Parameterize with CfnParameters or context values
- Tag all resources with environment, project, owner

## Code Quality
- Use L2/L3 constructs over L1 (Cfn*) when available
- Write unit tests with assertions library
- Use cdk diff before deploy
- Store secrets in Secrets Manager, not environment variables

## Deployment
- Use cdk bootstrap for each account/region
- Use --require-approval for production
- Enable termination protection on production stacks
- Use change sets for critical updates
"""

PRE_DEPLOY_VALIDATION = """# CloudFormation Pre-Deploy Validation
## Steps
1. **Lint**: `cfn-lint template.yaml` - syntax and schema validation
2. **Guard**: `cfn-guard validate -d template.yaml -r rules.guard` - compliance rules
3. **Change Set**: Create and review before executing
   ```
   aws cloudformation create-change-set \\
     --stack-name MyStack \\
     --template-body file://template.yaml \\
     --change-set-name review-changes
   aws cloudformation describe-change-set \\
     --stack-name MyStack \\
     --change-set-name review-changes
   ```
4. **Review**: Check for replacements, deletions, and scope of changes
5. **Execute**: `aws cloudformation execute-change-set --change-set-name review-changes --stack-name MyStack`
"""


def validate_cfn_template(template_content, region='ap-northeast-2', role_arn=None):
    """Validate CloudFormation template using AWS API. / AWS API를 사용하여 CloudFormation 템플릿을 검증합니다."""
    # Create CloudFormation client / CloudFormation 클라이언트 생성
    cfn = get_client('cloudformation', region, role_arn)
    try:
        # Call AWS CloudFormation validate API / AWS CloudFormation 검증 API 호출
        resp = cfn.validate_template(TemplateBody=template_content)
        return {
            'valid': True,
            'description': resp.get('Description', ''),
            'parameters': [{'key': p['ParameterKey'], 'default': p.get('DefaultValue', '')}
                           for p in resp.get('Parameters', [])],
            'capabilities': resp.get('Capabilities', []),
            'resources_count': template_content.count('Type: AWS::') + template_content.count('"Type": "AWS::'),
        }
    except Exception as e:
        # Return validation failure details / 검증 실패 세부 정보 반환
        return {'valid': False, 'error': str(e)}


def troubleshoot_cfn_deployment(stack_name, region='ap-northeast-2', role_arn=None):
    """Troubleshoot CloudFormation deployment failures. / CloudFormation 배포 실패를 진단합니다."""
    cfn = get_client('cloudformation', region, role_arn)
    try:
        # Get stack info / 스택 정보 조회
        stack = cfn.describe_stacks(StackName=stack_name)['Stacks'][0]
        status = stack['StackStatus']

        # Get failed events / 실패한 이벤트 조회
        events = cfn.describe_stack_events(StackName=stack_name)['StackEvents']
        failed = [e for e in events if 'FAILED' in e.get('ResourceStatus', '')]

        # Collect up to 10 failure details / 최대 10개의 실패 세부 정보 수집
        failures = []
        for e in failed[:10]:
            failures.append({
                'resource': e.get('LogicalResourceId', ''),
                'type': e.get('ResourceType', ''),
                'status': e.get('ResourceStatus', ''),
                'reason': e.get('ResourceStatusReason', ''),
                'timestamp': str(e.get('Timestamp', '')),
            })

        result = {
            'stackName': stack_name,
            'status': status,
            'statusReason': stack.get('StackStatusReason', ''),
            'failures': failures,
            'consoleUrl': 'https://{}.console.aws.amazon.com/cloudformation/home?region={}#/stacks/stackinfo?stackId={}'.format(
                region, region, stack.get('StackId', '')),
        }

        # Match common failure patterns and suggest fixes / 일반적인 실패 패턴을 매칭하고 해결 방법 제안
        for f in failures:
            reason = f['reason'].lower()
            if 'already exists' in reason:
                f['suggestion'] = 'Resource already exists. Use import or change logical ID.'
            elif 'limit' in reason or 'quota' in reason:
                f['suggestion'] = 'Service quota exceeded. Request increase via Service Quotas.'
            elif 'permission' in reason or 'denied' in reason:
                f['suggestion'] = 'IAM permission issue. Check execution role policies.'
            elif 'not found' in reason:
                f['suggestion'] = 'Referenced resource not found. Check cross-stack exports.'

        return result
    except Exception as e:
        # Return error if stack lookup fails / 스택 조회 실패 시 오류 반환
        return {'error': str(e)}


def search_cfn_docs(query):
    """Search CloudFormation documentation via AWS Knowledge MCP. / AWS Knowledge MCP를 통해 CloudFormation 문서를 검색합니다."""
    # Build JSON-RPC request payload / JSON-RPC 요청 페이로드 구성
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "aws___search_documentation",
                   "arguments": {"search_phrase": "CloudFormation " + query, "limit": 5}}
    }).encode()
    # Send request to AWS Knowledge MCP endpoint / AWS Knowledge MCP 엔드포인트에 요청 전송
    req = urllib.request.Request("https://knowledge-mcp.global.api.aws",
        data=payload, headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 - fixed https AWS Knowledge MCP endpoint literal, not user-controlled
        data = json.loads(resp.read().decode())
    # Extract text content from MCP response / MCP 응답에서 텍스트 콘텐츠 추출
    content = data.get("result", {}).get("content", [])
    return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")


def search_cdk_docs(query, language='typescript'):
    """Search CDK documentation via AWS Knowledge MCP. / AWS Knowledge MCP를 통해 CDK 문서를 검색합니다."""
    # Build JSON-RPC request payload / JSON-RPC 요청 페이로드 구성
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "aws___search_documentation",
                   "arguments": {"search_phrase": "CDK {} {}".format(language, query), "limit": 5}}
    }).encode()
    # Send request to AWS Knowledge MCP endpoint / AWS Knowledge MCP 엔드포인트에 요청 전송
    req = urllib.request.Request("https://knowledge-mcp.global.api.aws",
        data=payload, headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 - fixed https AWS Knowledge MCP endpoint literal, not user-controlled
        data = json.loads(resp.read().decode())
    # Extract text content from MCP response / MCP 응답에서 텍스트 콘텐츠 추출
    content = data.get("result", {}).get("content", [])
    return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")


def read_doc_page(url, max_length=10000):
    """Fetch documentation page and convert to simple text. / 문서 페이지를 가져와서 단순 텍스트로 변환합니다."""
    # Reject non-http(s) schemes and non-public destinations to prevent local
    # file reads and SSRF into internal/metadata endpoints.
    # http(s) 외 스킴 및 비공개 목적지 차단하여 로컬 파일 / SSRF 접근 방지
    _assert_public_http_url(url)
    # Fetch raw HTML from URL / URL에서 원시 HTML 가져오기
    req = urllib.request.Request(url, headers={'User-Agent': 'AWSops-IaC-MCP/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 - scheme validated to http/https above
        raw = resp.read().decode('utf-8', errors='replace')
    # HTML to text via stdlib parser (drops script/style, unescapes entities)
    # 표준 HTML 파서로 텍스트 변환 (스크립트/스타일 제거, 엔티티 복원)
    parser = _TextExtractor()
    parser.feed(raw)
    text = re.sub(r'\s+', ' ', parser.get_text()).strip()
    return text[:max_length]


def lambda_handler(event, context):
    """Lambda entry point for IaC MCP tools. / IaC MCP 도구의 람다 진입점."""
    # Parse event — Gateway unwraps arguments directly into event
    params = event if isinstance(event, dict) else json.loads(event)

    # Tool name: context.client_context.custom['bedrockAgentCoreToolName']
    tool_name = ""
    if hasattr(context, 'client_context') and context.client_context:
        custom = getattr(context.client_context, 'custom', None) or {}
        raw_name = custom.get('bedrockAgentCoreToolName', '')
        tool_name = raw_name.split('___')[-1] if '___' in raw_name else raw_name

    # Fallback: event에서 tool_name 또는 파라미터 기반 auto-detect
    if not tool_name:
        tool_name = params.get("tool_name", "")
    args = params.get("arguments", params) if "arguments" in params else params
    target_account_id = args.pop('target_account_id', None)
    role_arn = get_role_arn(target_account_id) if target_account_id else None

    # Infer tool from parameters if not specified / 도구명이 없으면 파라미터로 추론
    if not tool_name:
        if "template_content" in args:
            tool_name = "validate_cloudformation_template"
        elif "stack_name" in args:
            tool_name = "troubleshoot_cloudformation_deployment"
        elif "query" in args and "cdk" in args.get("query", "").lower():
            tool_name = "search_cdk_documentation"
        elif "query" in args:
            tool_name = "search_cloudformation_documentation"
        elif "url" in args:
            tool_name = "read_iac_documentation_page"
        else:
            tool_name = "cdk_best_practices"

    # Tool handler: validate CloudFormation template / 도구 핸들러: CloudFormation 템플릿 검증
    if tool_name == "validate_cloudformation_template":
        result = validate_cfn_template(args.get("template_content", ""), args.get("region") or default_region(), role_arn)
        return {"statusCode": 200, "body": json.dumps(result, default=str)}

    # Tool handler: check template compliance / 도구 핸들러: 템플릿 규정 준수 확인
    elif tool_name == "check_cloudformation_template_compliance":
        # Run validate + basic security checks / 검증 실행 + 기본 보안 점검
        tmpl = args.get("template_content", "")
        issues = []
        if "AWS::IAM" in tmpl and "*" in tmpl:
            issues.append({"severity": "HIGH", "rule": "no-wildcard-iam", "message": "Wildcard (*) found in IAM policy"})
        if "PubliclyAccessible" in tmpl and "true" in tmpl.lower():
            issues.append({"severity": "HIGH", "rule": "no-public-access", "message": "PubliclyAccessible is true"})
        if "Encrypted" not in tmpl and ("AWS::RDS" in tmpl or "AWS::EBS" in tmpl):
            issues.append({"severity": "MEDIUM", "rule": "encryption-required", "message": "Encryption not explicitly enabled"})
        validation = validate_cfn_template(tmpl, default_region(), role_arn)
        return {"statusCode": 200, "body": json.dumps({"validation": validation, "compliance_issues": issues}, default=str)}

    # Tool handler: troubleshoot deployment failures / 도구 핸들러: 배포 실패 문제 해결
    elif tool_name == "troubleshoot_cloudformation_deployment":
        result = troubleshoot_cfn_deployment(args.get("stack_name", ""), args.get("region") or default_region(), role_arn)
        return {"statusCode": 200, "body": json.dumps(result, default=str)}

    # Tool handler: pre-deploy validation instructions / 도구 핸들러: 배포 전 검증 안내
    elif tool_name == "get_cloudformation_pre_deploy_validation_instructions":
        return {"statusCode": 200, "body": PRE_DEPLOY_VALIDATION}

    # Tool handler: search CDK documentation / 도구 핸들러: CDK 문서 검색
    elif tool_name == "search_cdk_documentation":
        result = search_cdk_docs(args.get("query", ""), args.get("language", "typescript"))
        return {"statusCode": 200, "body": result[:50000]}

    # Tool handler: search CloudFormation documentation / 도구 핸들러: CloudFormation 문서 검색
    elif tool_name == "search_cloudformation_documentation":
        result = search_cfn_docs(args.get("query", ""))
        return {"statusCode": 200, "body": result[:50000]}

    # Tool handler: search CDK samples and constructs / 도구 핸들러: CDK 샘플 및 구성 요소 검색
    elif tool_name == "search_cdk_samples_and_constructs":
        result = search_cdk_docs(args.get("query", "") + " example sample", args.get("language", "typescript"))
        return {"statusCode": 200, "body": result[:50000]}

    # Tool handler: CDK best practices / 도구 핸들러: CDK 모범 사례
    elif tool_name == "cdk_best_practices":
        return {"statusCode": 200, "body": CDK_BEST_PRACTICES}

    # Tool handler: read IaC documentation page / 도구 핸들러: IaC 문서 페이지 읽기
    elif tool_name == "read_iac_documentation_page":
        try:
            text = read_doc_page(args.get("url", ""), args.get("max_length", 10000))
            return {"statusCode": 200, "body": text}
        except Exception as e:
            # Return error if page fetch fails / 페이지 가져오기 실패 시 오류 반환
            return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

    # Unknown tool error / 알 수 없는 도구 오류
    return {"statusCode": 400, "body": json.dumps({"error": "Unknown tool: " + tool_name})}
