"""VPC Reachability Analyzer Lambda / VPC 도달성 분석 Lambda"""
# VPC 리소스 간 네트워크 도달성 분석 / Analyze network reachability between VPC resources
import boto3, json
from cross_account import get_client, get_role_arn

def lambda_handler(event, context):
    # Parse event — Gateway unwraps arguments directly into event
    params = event if isinstance(event, dict) else json.loads(event)
    args = params.get("arguments", params) if "arguments" in params else params
    target_account_id = args.pop('target_account_id', None)
    role_arn = get_role_arn(target_account_id) if target_account_id else None
    region = args.get("region", "ap-northeast-2")
    ec2 = get_client('ec2', region, role_arn)
    source = args['source']
    destination = args['destination']
    protocol = args.get('protocol', 'tcp')
    port = args.get('port', 443)

    # 네트워크 인사이트 경로 생성 / Create network insights path
    path_resp = ec2.create_network_insights_path(
        Source=source, Destination=destination,
        Protocol=protocol, DestinationPort=int(port),
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
