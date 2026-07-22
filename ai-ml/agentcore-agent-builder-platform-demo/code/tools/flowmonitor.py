"""VPC Flow Log Monitor Lambda / VPC 흐름 로그 모니터링 Lambda"""
# VPC 흐름 로그 조회 및 분석 / Query and analyze VPC flow logs
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
    vpc_id = args['vpc_id']

    # 흐름 로그 조회 / Query flow logs
    resp = ec2.describe_flow_logs(Filters=[{'Name': 'resource-id', 'Values': [vpc_id]}])
    flow_logs = [{'FlowLogId': fl.get('FlowLogId'), 'ResourceId': fl.get('ResourceId'),
        'TrafficType': fl.get('TrafficType'), 'LogStatus': fl.get('LogStatus'),
        'LogDestination': fl.get('LogDestination')} for fl in resp.get('FlowLogs', [])]

    return {'statusCode': 200, 'body': json.dumps({
        'vpcId': vpc_id, 'flowLogs': flow_logs, 'count': len(flow_logs)})}
