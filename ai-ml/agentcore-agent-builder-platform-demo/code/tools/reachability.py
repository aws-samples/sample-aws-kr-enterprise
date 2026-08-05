"""VPC Reachability Analyzer Lambda / VPC 도달성 분석 Lambda"""
# VPC 리소스 간 네트워크 도달성 분석 / Analyze network reachability between VPC resources
import boto3, json
from cross_account import get_client, get_role_arn, default_region


def _find_existing_path(ec2, source, destination, protocol, port):
    """Find a reusable Network Insights path matching the same tuple.

    Reachability paths are static definitions; creating a fresh one on every
    invocation leaks them toward the per-account quota (no Delete permission is
    granted). Reuse an existing awsops-tagged path for the same
    source/destination/protocol/port instead of creating another.
    동일 튜플의 기존 경로를 재사용하여 경로 누수/쿼터 소진 방지."""
    try:
        paths = ec2.describe_network_insights_paths(
            Filters=[
                {"Name": "source", "Values": [source]},
                {"Name": "destination", "Values": [destination]},
                {"Name": "protocol", "Values": [protocol]},
            ]
        ).get("NetworkInsightsPaths", [])
    except Exception:
        return None
    for p in paths:
        if p.get("DestinationPort") == port:
            return p["NetworkInsightsPathId"]
    return None


def lambda_handler(event, context):
    # Parse event — Gateway unwraps arguments directly into event
    params = event if isinstance(event, dict) else json.loads(event)
    args = params.get("arguments", params) if "arguments" in params else params
    target_account_id = args.pop('target_account_id', None)
    role_arn = get_role_arn(target_account_id) if target_account_id else None
    region = args.get("region") or default_region()
    ec2 = get_client('ec2', region, role_arn)
    source = args['source']
    destination = args['destination']
    protocol = args.get('protocol', 'tcp')
    port = int(args.get('port', 443))

    try:
        # 기존 경로 재사용, 없으면 생성 / Reuse an existing path, otherwise create one
        path_id = _find_existing_path(ec2, source, destination, protocol, port)
        if not path_id:
            path_resp = ec2.create_network_insights_path(
                Source=source, Destination=destination,
                Protocol=protocol, DestinationPort=port,
                TagSpecifications=[{'ResourceType': 'network-insights-path',
                    'Tags': [{'Key': 'CreatedBy', 'Value': 'awsops'}]}]
            )
            path_id = path_resp['NetworkInsightsPath']['NetworkInsightsPathId']

        # 분석 시작 / Start analysis
        analysis_resp = ec2.start_network_insights_analysis(NetworkInsightsPathId=path_id)
        analysis_id = analysis_resp['NetworkInsightsAnalysis']['NetworkInsightsAnalysisId']

        return {'statusCode': 200, 'body': json.dumps({
            'pathId': path_id, 'analysisId': analysis_id,
            'status': analysis_resp['NetworkInsightsAnalysis']['Status']})}
    except Exception as e:
        # 쿼터 소진 등 실패를 처리되지 않은 5xx 대신 명확한 오류로 반환 / Surface failures cleanly
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
