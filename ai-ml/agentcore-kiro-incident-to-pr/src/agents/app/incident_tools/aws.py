"""AWS investigation and report tools exposed to Kiro agents over MCP. All of them are read-only.

Agents never change AWS resources. A fix is a pull request to the workload repository (see repository.py), which a
person merges and the workload pipeline deploys.
"""
import json
import os

import boto3

from . import repository

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"))

cw_logs = boto3.client("logs", region_name=REGION)
cw_client = boto3.client("cloudwatch", region_name=REGION)
cloudtrail = boto3.client("cloudtrail", region_name=REGION)
config_client = boto3.client("config", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)
rds_client = boto3.client("rds", region_name=REGION)
asg_client = boto3.client("autoscaling", region_name=REGION)
ecs_client = boto3.client("ecs", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)


# ========================================================================
# Investigation Tools (Read-only)
# ========================================================================

def query_cloudwatch_logs(log_group: str, query_string: str, hours_back: int = 1) -> str:
    """Run CloudWatch Logs Insights query."""
    import time
    from datetime import datetime, timezone, timedelta
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_back)
    try:
        resp = cw_logs.start_query(logGroupName=log_group, startTime=int(start.timestamp()),
                                    endTime=int(end.timestamp()), queryString=query_string, limit=20)
        for _ in range(10):
            time.sleep(1)
            result = cw_logs.get_query_results(queryId=resp["queryId"])
            if result["status"] == "Complete":
                return json.dumps(result["results"][:10], default=str)
        return "Query timed out"
    except Exception as e:
        return f"Error: {e}"


def lookup_cloudtrail_events(resource_name: str = "", event_name: str = "", hours_back: int = 24) -> str:
    """Lookup CloudTrail events to find who made changes.

    Args:
        resource_name: Resource name or ID to filter (e.g. sg-abc123, i-abc123)
        event_name: API event name to filter (e.g. RevokeSecurityGroupEgress, StopInstances)
        hours_back: How many hours back to search
    """
    from datetime import datetime, timezone, timedelta
    try:
        kwargs = {"StartTime": datetime.now(timezone.utc) - timedelta(hours=hours_back),
                  "EndTime": datetime.now(timezone.utc), "MaxResults": 20}
        if resource_name:
            kwargs["LookupAttributes"] = [{"AttributeKey": "ResourceName", "AttributeValue": resource_name}]
        elif event_name:
            kwargs["LookupAttributes"] = [{"AttributeKey": "EventName", "AttributeValue": event_name}]
        resp = cloudtrail.lookup_events(**kwargs)
        events = [{"time": str(e.get("EventTime")), "event": e.get("EventName"),
                    "user": e.get("Username"), "source": e.get("EventSource"),
                    "resources": [r.get("ResourceName") for r in e.get("Resources", [])]}
                   for e in resp.get("Events", [])]

        # If searching by resource and no results, hint to try by event name
        if resource_name and not events:
            events.append({"HINT": f"No events found for resource '{resource_name}'. Try searching by event_name instead (e.g. 'RevokeSecurityGroupEgress', 'AuthorizeSecurityGroupEgress', 'ModifySecurityGroupRules')."})

        return json.dumps(events, default=str)
    except Exception as e:
        return f"Error: {e}"


def get_ec2_console_output(instance_id: str) -> str:
    """Get EC2 instance system log (console output) for boot/kernel errors.

    Args:
        instance_id: EC2 instance ID
    """
    try:
        resp = ec2.get_console_output(InstanceId=instance_id, Latest=True)
        output = resp.get("Output", "")
        # Return last 2000 chars (most relevant)
        return output[-2000:] if output else "No console output available"
    except Exception as e:
        return f"Error: {e}"


def get_config_history(resource_type: str, resource_id: str) -> str:
    """Get AWS Config resource configuration change history.

    Args:
        resource_type: AWS resource type (e.g. AWS::EC2::SecurityGroup, AWS::EC2::Instance)
        resource_id: Resource ID
    """
    try:
        resp = config_client.get_resource_config_history(
            resourceType=resource_type, resourceId=resource_id, limit=5)
        items = [{"time": str(i.get("configurationItemCaptureTime")),
                  "status": i.get("configurationItemStatus"),
                  "config": str(i.get("configuration", ""))[:500]}
                 for i in resp.get("configurationItems", [])]
        return json.dumps(items, default=str)
    except Exception as e:
        return f"Error: {e}"


def get_metric_data(namespace: str, metric_name: str, dimensions: str, hours_back: int = 1) -> str:
    """Get CloudWatch metric data points."""
    from datetime import datetime, timezone, timedelta
    try:
        dims = json.loads(dimensions) if isinstance(dimensions, str) else dimensions
        resp = cw_client.get_metric_data(
            MetricDataQueries=[{"Id": "m1", "MetricStat": {
                "Metric": {"Namespace": namespace, "MetricName": metric_name, "Dimensions": dims},
                "Period": 60, "Stat": "Maximum"}}],
            StartTime=datetime.now(timezone.utc) - timedelta(hours=hours_back),
            EndTime=datetime.now(timezone.utc))
        r = resp["MetricDataResults"][0]
        return json.dumps([{"time": str(t), "value": v} for t, v in zip(r["Timestamps"][:20], r["Values"][:20])], default=str)
    except Exception as e:
        return f"Error: {e}"


def describe_alarms(alarm_prefix: str = "") -> str:
    """Describe CloudWatch alarms."""
    try:
        kwargs = {"MaxRecords": 20}
        if alarm_prefix:
            kwargs["AlarmNamePrefix"] = alarm_prefix
        resp = cw_client.describe_alarms(**kwargs)
        return json.dumps([{"name": a["AlarmName"], "state": a["StateValue"], "reason": a.get("StateReason", "")}
                           for a in resp.get("MetricAlarms", [])], default=str)
    except Exception as e:
        return f"Error: {e}"


def list_metrics(namespace: str = "", metric_name: str = "") -> str:
    """List available CloudWatch metrics."""
    try:
        kwargs = {}
        if namespace: kwargs["Namespace"] = namespace
        if metric_name: kwargs["MetricName"] = metric_name
        resp = cw_client.list_metrics(**kwargs)
        return json.dumps([{"namespace": m["Namespace"], "name": m["MetricName"], "dimensions": m.get("Dimensions", [])}
                           for m in resp.get("Metrics", [])[:20]], default=str)
    except Exception as e:
        return f"Error: {e}"


def describe_instances(filters: str = "") -> str:
    """Describe EC2 instances."""
    try:
        kwargs = {"Filters": json.loads(filters)} if filters else {}
        resp = ec2.describe_instances(**kwargs)
        instances = []
        for r in resp["Reservations"]:
            for i in r["Instances"]:
                instances.append({"id": i["InstanceId"], "state": i["State"]["Name"],
                                  "type": i["InstanceType"], "az": i["Placement"]["AvailabilityZone"],
                                  "security_groups": [{"sg_id": sg["GroupId"], "sg_name": sg["GroupName"]} for sg in i.get("SecurityGroups", [])],
                                  "subnet_id": i.get("SubnetId", ""), "vpc_id": i.get("VpcId", "")})
        return json.dumps(instances[:20], default=str)
    except Exception as e:
        return f"Error: {e}"


def describe_target_health(target_group_arn: str = "") -> str:
    """Describe ALB target group health."""
    try:
        if not target_group_arn:
            tgs = elbv2.describe_target_groups(PageSize=10)
            if not tgs.get("TargetGroups"): return "No target groups found"
            target_group_arn = tgs["TargetGroups"][0]["TargetGroupArn"]
        resp = elbv2.describe_target_health(TargetGroupArn=target_group_arn)
        return json.dumps([{"id": t["Target"]["Id"], "port": t["Target"]["Port"],
                            "health": t["TargetHealth"]["State"], "reason": t["TargetHealth"].get("Reason", "")}
                           for t in resp["TargetHealthDescriptions"]], default=str)
    except Exception as e:
        return f"Error: {e}"


def check_security_groups(sg_id: str = "") -> str:
    """Check Security Group rules and which resources use this SG. Returns inbound/outbound rules plus attached resources. WARNING messages indicate misconfigurations."""
    try:
        kwargs = {"Filters": [{"Name": "group-id", "Values": [sg_id]}]} if sg_id else {}
        resp = ec2.describe_security_group_rules(**kwargs)
        rules = [{"rule_id": r.get("SecurityGroupRuleId", ""), "sg_id": r["GroupId"], "direction": "inbound" if not r["IsEgress"] else "outbound",
                            "protocol": r.get("IpProtocol", ""), "from_port": r.get("FromPort", ""),
                            "to_port": r.get("ToPort", ""),
                            "source": r.get("CidrIpv4", r.get("ReferencedGroupInfo", {}).get("GroupId", ""))}
                           for r in resp["SecurityGroupRules"][:50]]

        # Identify which resources use this SG
        if sg_id:
            sg_desc = ec2.describe_security_groups(GroupIds=[sg_id])
            for sg in sg_desc.get("SecurityGroups", []):
                sg_name = sg.get("GroupName", "")
                sg_desc_text = sg.get("Description", "")
                rules.insert(0, {"sg_id": sg_id, "sg_name": sg_name, "description": sg_desc_text})

            # Find attached ENIs to determine what resource owns this SG
            enis = ec2.describe_network_interfaces(Filters=[{"Name": "group-id", "Values": [sg_id]}])
            attached = []
            for eni in enis.get("NetworkInterfaces", [])[:10]:
                owner = eni.get("Description", "")
                attached.append({"eni_id": eni["NetworkInterfaceId"], "owner_description": owner,
                                 "attachment": eni.get("Attachment", {}).get("InstanceId", "N/A")})
            if attached:
                rules.insert(1, {"attached_resources": attached})

            has_egress = any(r.get("direction") == "outbound" for r in rules)
            has_ingress = any(r.get("direction") == "inbound" for r in rules)
            if not has_egress:
                rules.append({"WARNING": f"Security Group {sg_id} has NO EGRESS (outbound) rules — ALL outbound traffic from resources using this SG is BLOCKED."})
            if not has_ingress:
                rules.append({"WARNING": f"Security Group {sg_id} has NO INGRESS (inbound) rules — ALL inbound traffic to resources using this SG is BLOCKED."})

        return json.dumps(rules, default=str)
    except Exception as e:
        return f"Error: {e}"


def describe_db_instances(db_id: str = "") -> str:
    """Describe RDS instances."""
    try:
        kwargs = {"DBInstanceIdentifier": db_id} if db_id else {}
        resp = rds_client.describe_db_instances(**kwargs)
        return json.dumps([{"id": db["DBInstanceIdentifier"], "status": db["DBInstanceStatus"],
                            "engine": db["Engine"], "endpoint": db.get("Endpoint", {}).get("Address", "")}
                           for db in resp["DBInstances"][:10]], default=str)
    except Exception as e:
        return f"Error: {e}"


def describe_auto_scaling_groups(asg_name: str = "") -> str:
    """Describe Auto Scaling Groups."""
    try:
        kwargs = {"AutoScalingGroupNames": [asg_name]} if asg_name else {}
        resp = asg_client.describe_auto_scaling_groups(**kwargs)
        return json.dumps([{"name": a["AutoScalingGroupName"], "desired": a["DesiredCapacity"],
                            "min": a["MinSize"], "max": a["MaxSize"],
                            "instances": [{"id": i["InstanceId"], "state": i["LifecycleState"]} for i in a["Instances"]]}
                           for a in resp["AutoScalingGroups"][:10]], default=str)
    except Exception as e:
        return f"Error: {e}"


def describe_vpcs(vpc_id: str = "") -> str:
    """Describe VPCs."""
    try:
        kwargs = {"VpcIds": [vpc_id]} if vpc_id else {}
        resp = ec2.describe_vpcs(**kwargs)
        return json.dumps([{"id": v["VpcId"], "cidr": v["CidrBlock"], "state": v["State"]} for v in resp["Vpcs"][:10]], default=str)
    except Exception as e:
        return f"Error: {e}"


def describe_ecs_services(cluster: str = "") -> str:
    """Describe ECS services and tasks."""
    try:
        clusters = ecs_client.list_clusters()["clusterArns"][:5]
        if cluster:
            clusters = [c for c in clusters if cluster in c] or clusters[:1]
        result = []
        for c in clusters:
            svcs = ecs_client.list_services(cluster=c, maxResults=10).get("serviceArns", [])
            if svcs:
                details = ecs_client.describe_services(cluster=c, services=svcs[:5])
                for s in details.get("services", []):
                    result.append({"cluster": c.split("/")[-1], "service": s["serviceName"],
                                   "desired": s["desiredCount"], "running": s["runningCount"], "status": s["status"]})
        return json.dumps(result, default=str) if result else "No ECS services found"
    except Exception as e:
        return f"Error: {e}"


def describe_lambda_functions(function_name: str = "") -> str:
    """Describe Lambda functions."""
    try:
        if function_name:
            resp = lambda_client.get_function(FunctionName=function_name)
            c = resp["Configuration"]
            return json.dumps({"name": c["FunctionName"], "runtime": c.get("Runtime", ""), "memory": c["MemorySize"],
                               "timeout": c["Timeout"], "state": c.get("State", "Active")}, default=str)
        resp = lambda_client.list_functions(MaxItems=20)
        return json.dumps([{"name": f["FunctionName"], "runtime": f.get("Runtime", ""), "memory": f["MemorySize"],
                            "timeout": f["Timeout"]} for f in resp["Functions"]], default=str)
    except Exception as e:
        return f"Error: {e}"


def describe_nat_gateways(vpc_id: str = "") -> str:
    """Describe NAT Gateways."""
    try:
        kwargs = {"Filter": [{"Name": "vpc-id", "Values": [vpc_id]}]} if vpc_id else {}
        resp = ec2.describe_nat_gateways(**kwargs)
        return json.dumps([{"id": n["NatGatewayId"], "state": n["State"], "vpc": n.get("VpcId", ""),
                            "subnet": n.get("SubnetId", "")} for n in resp["NatGateways"][:10]], default=str)
    except Exception as e:
        return f"Error: {e}"


# ========================================================================
# Report/Alarm Query Tools (for Chatbot)
# ========================================================================

def _report_status(item):
    """Same rule as the API handler: the fix pull request decides once the report is written."""
    pull_request = item.get("report_data", {}).get("pull_request") or {}
    if item.get("status") in ("PENDING", "MERGED") and pull_request.get("pull_request_id"):
        state = repository.pull_request_state(pull_request)
        return {"DEPLOYED": "RESOLVED", "MERGED": "MERGED", "CLOSED": "REPORTED"}.get(state, "PENDING")
    return item.get("status")


def query_reports(date_filter: str = "", alarm_name: str = "", limit: int = 10) -> str:
    """Query RCA reports from DynamoDB."""
    table_name = os.environ.get("REPORTS_TABLE", "incident-to-pr-reports")
    try:
        ddb = boto3.resource("dynamodb", region_name=REGION)
        table = ddb.Table(table_name)
        items = table.scan().get("Items", [])
        if date_filter:
            items = [i for i in items if i.get("created_at", "").startswith(date_filter)]
        if alarm_name:
            items = [i for i in items if alarm_name.lower() in i.get("alarm_name", "").lower()]
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return json.dumps([{"report_id": i.get("report_id"), "created_at": i.get("created_at"),
                            "alarm_name": i.get("alarm_name"), "workload": i.get("report_data", {}).get("workload", ""),
                            "status": _report_status(i), "summary": i.get("report_data", {}).get("summary", "")[:200]}
                           for i in items[:limit]], default=str, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


def query_alarms(alarm_prefix: str = "", include_history: bool = False) -> str:
    """Query CloudWatch alarms and optionally their state change history."""
    try:
        kwargs = {"MaxRecords": 20}
        if alarm_prefix: kwargs["AlarmNamePrefix"] = alarm_prefix
        resp = cw_client.describe_alarms(**kwargs)
        alarms = [{"name": a["AlarmName"], "state": a["StateValue"], "reason": a.get("StateReason", ""),
                    "updated": str(a.get("StateUpdatedTimestamp", "")), "namespace": a.get("Namespace", ""),
                    "metric": a.get("MetricName", "")} for a in resp.get("MetricAlarms", [])]
        result = {"alarms": alarms}
        if include_history:
            history = []
            for a in alarms[:5]:
                for item in cw_client.describe_alarm_history(AlarmName=a["name"], HistoryItemType="StateUpdate", MaxRecords=5).get("AlarmHistoryItems", []):
                    history.append({"alarm": a["name"], "time": str(item.get("Timestamp")), "summary": item.get("HistorySummary", "")})
            result["history"] = history
        return json.dumps(result, default=str, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


LOGS = [query_cloudwatch_logs, lookup_cloudtrail_events, get_ec2_console_output, get_config_history]
METRICS = [get_metric_data, describe_alarms, list_metrics]
INFRASTRUCTURE = [describe_instances, describe_target_health, check_security_groups, describe_db_instances,
                  describe_auto_scaling_groups, describe_vpcs, describe_ecs_services, describe_lambda_functions,
                  describe_nat_gateways]
REPORTS = [query_reports, query_alarms]
