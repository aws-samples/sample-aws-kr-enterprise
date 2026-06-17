"""AIOps Specialist Agents - Strands Agents as Tools pattern."""

import json
import os
import boto3
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"))
KB_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")

# Shared model for all specialist agents. Without this, Agent() falls back to the
# Strands SDK default model (Claude Sonnet 4.0) instead of the configured MODEL_ID.
MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6")
model = BedrockModel(model_id=MODEL_ID, region_name=REGION)

# Boto3 clients
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
route53_client = boto3.client("route53", region_name=REGION)
bedrock_agent = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ========================================================================
# Specialist Agents (Agent as Tools)
# ========================================================================

# --- Logs Agent ---
@tool
def logs_agent(query: str, reason: str = "") -> str:
    """Analyze CloudWatch Logs, EC2 system logs, CloudTrail, and AWS Config for incident investigation.

    Args:
        query: Description of the incident to investigate via logs
        reason: Evidence-based justification for this investigation (e.g. "ALB alarm on app/MyALB/abc → checking CloudTrail for recent SG changes")
    """
    try:
        agent = Agent(
            model=model,
            system_prompt="""You are a Logs Agent. Investigate ONLY the specific resources and events described in the query.

RULES:
- Use ONLY the tools needed for the specific investigation requested
- Do NOT search broadly — target specific resource IDs, log groups, or event names from the query
- PARALLEL: If multiple independent searches are needed (e.g. CloudWatch Logs AND CloudTrail for the same resource), call them in the SAME turn
- Make MAXIMUM 3 tool calls total
- Return concise summary (max 800 tokens) including WHO changed WHAT and WHEN

Tools:
- query_cloudwatch_logs: Search specific log groups for errors
- lookup_cloudtrail_events: Search for API changes to specific resources
- get_ec2_console_output: Check specific instance system logs
- get_config_history: Check specific resource configuration changes""",
            tools=[query_cloudwatch_logs, lookup_cloudtrail_events, get_ec2_console_output, get_config_history],
            callback_handler=None,
        )
        prompt = f"Investigate: {query}"
        if reason:
            prompt = f"[Reason: {reason}]\n{prompt}"
        return str(agent(prompt))
    except Exception as e:
        return f"Logs agent error: {e}"


# --- Metrics Agent ---
@tool
def metrics_agent(query: str, reason: str = "") -> str:
    """Analyze CloudWatch Metrics for anomalies and trends.

    Args:
        query: Description of metrics to analyze
        reason: Evidence-based justification for this investigation (e.g. "ALB 4XX alarm → checking HTTPCode_Target_4XX_Count metric trend")
    """
    try:
        agent = Agent(
            model=model,
            system_prompt="""You are a Metrics Agent. Analyze ONLY the specific metrics described in the query.

RULES:
- Query only the metrics directly relevant to the investigation
- Do NOT explore unrelated namespaces or metrics
- PARALLEL: If multiple independent metrics are needed (e.g. 4XX count AND target response time), call get_metric_data for each in the SAME turn
- Make MAXIMUM 3 tool calls total
- Return concise summary (max 600 tokens) with actual metric values and timestamps""",
            tools=[get_metric_data, describe_alarms, list_metrics],
            callback_handler=None,
        )
        prompt = f"Analyze metrics: {query}"
        if reason:
            prompt = f"[Reason: {reason}]\n{prompt}"
        return str(agent(prompt))
    except Exception as e:
        return f"Metrics agent error: {e}"


# --- Infrastructure Agent ---
@tool
def infrastructure_agent(query: str, reason: str = "") -> str:
    """Investigate AWS resource states across EC2, ALB, RDS, SG, ASG, ECS, Lambda, VPC, Route53.

    Args:
        query: Description of infrastructure to investigate
        reason: Evidence-based justification for this investigation (e.g. "ALB target unhealthy → checking EC2 instance i-abc123 state")
    """
    try:
        agent = Agent(
            model=model,
            system_prompt="""You are an Infrastructure Agent. Check ONLY the specific resources mentioned in the query.

RULES:
- Investigate only the resource IDs or types explicitly requested
- Do NOT scan all resources of a type — check only the ones linked to the investigation
- PARALLEL: If multiple independent resources need checking (e.g. ALB target health AND ASG status), call them in the SAME turn
- For each resource checked, report HEALTHY or UNHEALTHY with specific evidence
- Make MAXIMUM 3 tool calls total
- Return concise summary (max 1000 tokens)""",
            tools=[describe_instances, describe_target_health, check_security_groups,
                   describe_db_instances, describe_auto_scaling_groups, describe_vpcs,
                   describe_ecs_services, describe_lambda_functions, describe_nat_gateways],
            callback_handler=None,
        )
        prompt = f"Investigate infrastructure: {query}"
        if reason:
            prompt = f"[Reason: {reason}]\n{prompt}"
        return str(agent(prompt))
    except Exception as e:
        return f"Infrastructure agent error: {e}"


# --- Knowledge Agent ---
@tool
def knowledge_agent(query: str, reason: str = "") -> str:
    """Search operational history and runbooks from Bedrock Knowledge Base.

    Args:
        query: Description of the incident to search for relevant knowledge
        reason: Evidence-based justification for this search (e.g. "ALB 4XX spike with healthy targets → searching for similar past incidents")
    """
    try:
        agent = Agent(
            model=model,
            system_prompt="You are a Knowledge Agent. Search for relevant runbooks, past incidents, and system specs matching the specific issue described. Return concise summary (max 600 tokens).",
            tools=[retrieve_from_kb],
            callback_handler=None,
        )
        prompt = f"Search knowledge base: {query}"
        if reason:
            prompt = f"[Reason: {reason}]\n{prompt}"
        return str(agent(prompt))
    except Exception as e:
        return f"Knowledge agent error: {e}"


# --- Remediation Agent (for Executor) ---
@tool
def remediation_agent(action_description: str, parameters: str) -> str:
    """Execute approved remediation actions on AWS resources.

    Args:
        action_description: Description of the action to execute
        parameters: JSON string of action parameters
    """
    try:
        agent = Agent(
            model=model,
            system_prompt="You are a Remediation Agent. Execute ONLY the specific approved action described. Record before/after state. Return execution results.",
            tools=[reboot_instance, start_instance, stop_instance,
                   modify_security_group_ingress, modify_security_group_egress,
                   set_asg_capacity, suspend_asg_processes, resume_asg_processes,
                   reboot_db_instance, register_targets, deregister_targets,
                   update_ecs_service, stop_ecs_task, update_lambda_config,
                   set_alarm_state],
            callback_handler=None,
        )
        return str(agent(f"Execute: {action_description}\nParameters: {parameters}"))
    except Exception as e:
        return f"Remediation agent error: {e}"


# ========================================================================
# Investigation Tools (Read-only)
# ========================================================================

@tool
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


@tool
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


@tool
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


@tool
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


@tool
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


@tool
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


@tool
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


@tool
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


@tool
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


@tool
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


@tool
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


@tool
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


@tool
def describe_vpcs(vpc_id: str = "") -> str:
    """Describe VPCs."""
    try:
        kwargs = {"VpcIds": [vpc_id]} if vpc_id else {}
        resp = ec2.describe_vpcs(**kwargs)
        return json.dumps([{"id": v["VpcId"], "cidr": v["CidrBlock"], "state": v["State"]} for v in resp["Vpcs"][:10]], default=str)
    except Exception as e:
        return f"Error: {e}"


@tool
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


@tool
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


@tool
def describe_nat_gateways(vpc_id: str = "") -> str:
    """Describe NAT Gateways."""
    try:
        kwargs = {"Filter": [{"Name": "vpc-id", "Values": [vpc_id]}]} if vpc_id else {}
        resp = ec2.describe_nat_gateways(**kwargs)
        return json.dumps([{"id": n["NatGatewayId"], "state": n["State"], "vpc": n.get("VpcId", ""),
                            "subnet": n.get("SubnetId", "")} for n in resp["NatGateways"][:10]], default=str)
    except Exception as e:
        return f"Error: {e}"


@tool
def retrieve_from_kb(query: str) -> str:
    """Search Bedrock Knowledge Base for runbooks and operational history."""
    if not KB_ID: return "Knowledge Base ID not configured"
    try:
        resp = bedrock_agent.retrieve(knowledgeBaseId=KB_ID, retrievalQuery={"text": query},
                                       retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 5}})
        return json.dumps([{"text": r["content"]["text"], "score": r.get("score", 0)}
                           for r in resp.get("retrievalResults", [])], default=str)
    except Exception as e:
        return f"Error: {e}"


# ========================================================================
# Remediation Tools (Write actions - specific APIs only)
# ========================================================================

@tool
def reboot_instance(instance_id: str) -> str:
    """Reboot an EC2 instance."""
    try:
        ec2.reboot_instances(InstanceIds=[instance_id])
        return f"Success: rebooted {instance_id}"
    except Exception as e:
        return f"Error: {e}"

@tool
def start_instance(instance_id: str) -> str:
    """Start a stopped EC2 instance."""
    try:
        ec2.start_instances(InstanceIds=[instance_id])
        return f"Success: started {instance_id}"
    except Exception as e:
        return f"Error: {e}"

@tool
def stop_instance(instance_id: str) -> str:
    """Stop an EC2 instance."""
    try:
        ec2.stop_instances(InstanceIds=[instance_id])
        return f"Success: stopped {instance_id}"
    except Exception as e:
        return f"Error: {e}"

@tool
def modify_security_group_ingress(sg_id: str, action: str, protocol: str, port: int, source: str) -> str:
    """Authorize or revoke Security Group inbound rule.

    Args:
        sg_id: Security Group ID
        action: "authorize" or "revoke"
        protocol: "tcp", "udp", or "-1" for all traffic
        port: Port number (0 for all when protocol is -1)
        source: Source CIDR or SG ID
    """
    try:
        perm = {"IpProtocol": protocol}
        if protocol != "-1":
            perm["FromPort"] = port
            perm["ToPort"] = port
        if source.startswith("sg-"):
            perm["UserIdGroupPairs"] = [{"GroupId": source}]
        else:
            perm["IpRanges"] = [{"CidrIp": source}]
        if action == "authorize":
            ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[perm])
        else:
            ec2.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=[perm])
        return f"Success: {action} inbound {protocol}:{port} from {source} on {sg_id}"
    except Exception as e:
        return f"Error: {e}"

@tool
def modify_security_group_egress(sg_id: str, action: str, protocol: str, port: int, destination: str) -> str:
    """Authorize or revoke Security Group outbound rule.

    Args:
        sg_id: Security Group ID
        action: "authorize" or "revoke"
        protocol: "tcp", "udp", or "-1" for all traffic
        port: Port number (0 for all when protocol is -1)
        destination: Destination CIDR (e.g. 0.0.0.0/0) or Security Group ID (e.g. sg-xxx)
    """
    try:
        perm = {"IpProtocol": protocol}
        if protocol != "-1":
            perm["FromPort"] = port
            perm["ToPort"] = port
        if destination.startswith("sg-"):
            perm["UserIdGroupPairs"] = [{"GroupId": destination}]
        else:
            perm["IpRanges"] = [{"CidrIp": destination}]
        if action == "authorize":
            ec2.authorize_security_group_egress(GroupId=sg_id, IpPermissions=[perm])
        else:
            ec2.revoke_security_group_egress(GroupId=sg_id, IpPermissions=[perm])
        return f"Success: {action} outbound {protocol}:{port} to {destination} on {sg_id}"
    except Exception as e:
        return f"Error: {e}"

@tool
def set_asg_capacity(asg_name: str, desired_capacity: int) -> str:
    """Set Auto Scaling Group desired capacity."""
    try:
        asg_client.set_desired_capacity(AutoScalingGroupName=asg_name, DesiredCapacity=desired_capacity)
        return f"Success: set {asg_name} desired capacity to {desired_capacity}"
    except Exception as e:
        return f"Error: {e}"

@tool
def suspend_asg_processes(asg_name: str) -> str:
    """Suspend Auto Scaling Group processes."""
    try:
        asg_client.suspend_processes(AutoScalingGroupName=asg_name)
        return f"Success: suspended processes for {asg_name}"
    except Exception as e:
        return f"Error: {e}"

@tool
def resume_asg_processes(asg_name: str) -> str:
    """Resume Auto Scaling Group processes."""
    try:
        asg_client.resume_processes(AutoScalingGroupName=asg_name)
        return f"Success: resumed processes for {asg_name}"
    except Exception as e:
        return f"Error: {e}"

@tool
def reboot_db_instance(db_instance_id: str) -> str:
    """Reboot an RDS instance."""
    try:
        rds_client.reboot_db_instance(DBInstanceIdentifier=db_instance_id)
        return f"Success: rebooted RDS {db_instance_id}"
    except Exception as e:
        return f"Error: {e}"

@tool
def register_targets(target_group_arn: str, targets: str) -> str:
    """Register targets to ALB target group. targets: JSON array of {Id, Port}."""
    try:
        elbv2.register_targets(TargetGroupArn=target_group_arn, Targets=json.loads(targets))
        return f"Success: registered targets to {target_group_arn}"
    except Exception as e:
        return f"Error: {e}"

@tool
def deregister_targets(target_group_arn: str, targets: str) -> str:
    """Deregister targets from ALB target group. targets: JSON array of {Id, Port}."""
    try:
        elbv2.deregister_targets(TargetGroupArn=target_group_arn, Targets=json.loads(targets))
        return f"Success: deregistered targets from {target_group_arn}"
    except Exception as e:
        return f"Error: {e}"

@tool
def update_ecs_service(cluster: str, service: str, desired_count: int) -> str:
    """Update ECS service desired count."""
    try:
        ecs_client.update_service(cluster=cluster, service=service, desiredCount=desired_count)
        return f"Success: set {service} desired count to {desired_count}"
    except Exception as e:
        return f"Error: {e}"

@tool
def stop_ecs_task(cluster: str, task: str) -> str:
    """Stop an ECS task."""
    try:
        ecs_client.stop_task(cluster=cluster, task=task, reason="AIOps remediation")
        return f"Success: stopped task {task}"
    except Exception as e:
        return f"Error: {e}"

@tool
def update_lambda_config(function_name: str, memory_size: int = 0, timeout: int = 0) -> str:
    """Update Lambda function configuration (memory and/or timeout)."""
    try:
        kwargs = {"FunctionName": function_name}
        if memory_size > 0: kwargs["MemorySize"] = memory_size
        if timeout > 0: kwargs["Timeout"] = timeout
        lambda_client.update_function_configuration(**kwargs)
        return f"Success: updated {function_name} config"
    except Exception as e:
        return f"Error: {e}"

@tool
def set_alarm_state(alarm_name: str, state: str, reason: str = "Reset by AIOps") -> str:
    """Set CloudWatch alarm state (e.g. reset to OK)."""
    try:
        cw_client.set_alarm_state(AlarmName=alarm_name, StateValue=state, StateReason=reason)
        return f"Success: set {alarm_name} to {state}"
    except Exception as e:
        return f"Error: {e}"


# ========================================================================
# Report/Alarm Query Tools (for Chatbot)
# ========================================================================

@tool
def query_reports(date_filter: str = "", alarm_name: str = "", limit: int = 10) -> str:
    """Query RCA reports from DynamoDB."""
    table_name = os.environ.get("REPORTS_TABLE", "aiops-demo-reports")
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
                            "status": i.get("status"), "summary": i.get("report_data", {}).get("summary", "")[:200]}
                           for i in items[:limit]], default=str, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


@tool
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
