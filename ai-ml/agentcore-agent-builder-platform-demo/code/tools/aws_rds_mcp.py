"""
AWS RDS MCP Lambda - MySQL/PostgreSQL instance management, queries via RDS Data API
AWS RDS MCP 람다 - MySQL/PostgreSQL 인스턴스 관리, RDS Data API를 통한 쿼리
"""
import json
import re
from cross_account import get_client, get_role_arn, default_region

# Write / permission / procedure keywords that must never run through the
# read-only Data API path. Matched as whole words regardless of surrounding
# punctuation (e.g. "1;DELETE", "GRANT ALL"). / 쓰기·권한·프로시저 키워드 차단.
_WRITE_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "replace", "grant", "revoke", "call", "merge", "rename",
    "exec", "execute", "load", "lock", "attach",
    "into",  # SELECT ... INTO writes to a new table/file
}
# Extract SQL word tokens, ignoring punctuation glue. / 구두점 무시하고 SQL 단어 토큰 추출.
_SQL_WORD_RE = re.compile(r"[A-Za-z_]+")


def lambda_handler(event, context):
    # Parse event — Gateway unwraps arguments directly into event
    params = event if isinstance(event, dict) else json.loads(event)

    # Tool name: context.client_context.custom['bedrockAgentCoreToolName']
    t = ""
    if hasattr(context, 'client_context') and context.client_context:
        custom = getattr(context.client_context, 'custom', None) or {}
        raw_name = custom.get('bedrockAgentCoreToolName', '')
        t = raw_name.split('___')[-1] if '___' in raw_name else raw_name

    # Fallback: event에서 tool_name 또는 파라미터 기반 auto-detect
    if not t:
        t = params.get("tool_name", "")
    args = params.get("arguments", params) if "arguments" in params else params
    target_account_id = args.pop('target_account_id', None)
    role_arn = get_role_arn(target_account_id) if target_account_id else None
    region = args.get("region") or default_region()

    # Auto-detect tool from parameters if not specified / tool_name 미지정 시 파라미터로 도구 자동 감지
    if not t:
        if "sql" in args and "resource_arn" in args: t = "execute_sql"
        elif "db_instance_identifier" in args: t = "describe_db_instance"
        elif "db_cluster_identifier" in args: t = "describe_db_cluster"
        else: t = "list_db_instances"

    try:
        rds = get_client('rds', region, role_arn)

        # List all RDS instances with basic info / 모든 RDS 인스턴스 기본 정보 조회
        if t == "list_db_instances":
            # Describe all DB instances / 모든 DB 인스턴스 조회
            instances = rds.describe_db_instances().get("DBInstances", [])
            return ok({"instances": [{"id": i["DBInstanceIdentifier"], "engine": i["Engine"],
                "version": i.get("EngineVersion"), "class": i["DBInstanceClass"],
                "status": i["DBInstanceStatus"], "az": i.get("AvailabilityZone"),
                "multiAZ": i.get("MultiAZ"), "storage": i.get("AllocatedStorage"),
                "endpoint": i.get("Endpoint", {}).get("Address", "")}
                for i in instances[:20]]})

        # List all Aurora DB clusters / 모든 Aurora DB 클러스터 조회
        elif t == "list_db_clusters":
            clusters = rds.describe_db_clusters().get("DBClusters", [])
            return ok({"clusters": [{"id": c["DBClusterIdentifier"], "engine": c["Engine"],
                "version": c.get("EngineVersion"), "status": c["Status"],
                "members": len(c.get("DBClusterMembers", [])),
                "endpoint": c.get("Endpoint", ""), "readerEndpoint": c.get("ReaderEndpoint", "")}
                for c in clusters[:20]]})

        # Get detailed DB instance info / DB 인스턴스 상세 정보 조회
        elif t == "describe_db_instance":
            # Fetch instance details including networking and encryption / 네트워킹·암호화 포함 인스턴스 상세 조회
            i = rds.describe_db_instances(DBInstanceIdentifier=args["db_instance_identifier"])["DBInstances"][0]
            return ok({"id": i["DBInstanceIdentifier"], "engine": i["Engine"], "version": i.get("EngineVersion"),
                "class": i["DBInstanceClass"], "status": i["DBInstanceStatus"],
                "az": i.get("AvailabilityZone"), "multiAZ": i.get("MultiAZ"),
                "storage": i.get("AllocatedStorage"), "storageType": i.get("StorageType"),
                "encrypted": i.get("StorageEncrypted"), "vpcId": i.get("DBSubnetGroup", {}).get("VpcId"),
                "endpoint": i.get("Endpoint", {}).get("Address", ""),
                "port": i.get("Endpoint", {}).get("Port"),
                "securityGroups": [sg["VpcSecurityGroupId"] for sg in i.get("VpcSecurityGroups", [])],
                "parameterGroup": i.get("DBParameterGroups", [{}])[0].get("DBParameterGroupName", ""),
                "backupRetention": i.get("BackupRetentionPeriod"),
                "publiclyAccessible": i.get("PubliclyAccessible")})

        # Get detailed Aurora cluster info / Aurora 클러스터 상세 정보 조회
        elif t == "describe_db_cluster":
            c = rds.describe_db_clusters(DBClusterIdentifier=args["db_cluster_identifier"])["DBClusters"][0]
            return ok({"id": c["DBClusterIdentifier"], "engine": c["Engine"], "version": c.get("EngineVersion"),
                "status": c["Status"], "endpoint": c.get("Endpoint"), "readerEndpoint": c.get("ReaderEndpoint"),
                "port": c.get("Port"), "encrypted": c.get("StorageEncrypted"),
                "members": [{"id": m["DBInstanceIdentifier"], "writer": m.get("IsClusterWriter")}
                    for m in c.get("DBClusterMembers", [])],
                "backupRetention": c.get("BackupRetentionPeriod"),
                "deletionProtection": c.get("DeletionProtection")})

        # Execute read-only SQL via RDS Data API / RDS Data API를 통한 읽기 전용 SQL 실행
        elif t == "execute_sql":
            rds_data = get_client('rds-data', region, role_arn)
            # Block write operations (read-only enforcement) / 쓰기 작업 차단 (읽기 전용 강제)
            sql = args["sql"].strip()
            # Reject stacked statements — anything after the first ';' could be a
            # write hidden behind a SELECT (e.g. "SELECT 1;DELETE FROM users").
            # 세미콜론으로 이어붙인 다중 문(스택 쿼리) 거부.
            if ";" in sql.rstrip().rstrip(";"):
                return err("Only a single SELECT statement is allowed")
            words = [w.lower() for w in _SQL_WORD_RE.findall(sql)]
            # Must start with SELECT/WITH and contain no write/permission keyword. / SELECT·WITH로 시작하고 쓰기 키워드 없어야 함.
            if not words or words[0] not in ("select", "with"):
                return err("Only SELECT queries allowed")
            if set(words) & _WRITE_KEYWORDS:
                return err("Only SELECT queries allowed")
            # Execute SQL statement via Data API / Data API로 SQL 문 실행
            resp = rds_data.execute_statement(
                resourceArn=args["resource_arn"], secretArn=args["secret_arn"],
                database=args.get("database", ""), sql=sql)
            columns = [c.get("label", c.get("name", "")) for c in resp.get("columnMetadata", [])]
            rows = []
            for record in resp.get("records", [])[:100]:
                row = {}
                for i, field in enumerate(record):
                    col = columns[i] if i < len(columns) else "col{}".format(i)
                    val = field.get("stringValue", field.get("longValue", field.get("doubleValue",
                        field.get("booleanValue", field.get("isNull", None)))))
                    row[col] = val
                rows.append(row)
            return ok({"columns": columns, "rows": rows, "rowCount": len(rows)})

        # List DB snapshots (automated or manual) / DB 스냅샷 목록 조회 (자동 또는 수동)
        elif t == "list_snapshots":
            snaps = rds.describe_db_snapshots(SnapshotType=args.get("snapshot_type", "automated")).get("DBSnapshots", [])
            return ok({"snapshots": [{"id": s["DBSnapshotIdentifier"], "instance": s.get("DBInstanceIdentifier"),
                "engine": s["Engine"], "status": s["Status"], "size": s.get("AllocatedStorage"),
                "created": str(s.get("SnapshotCreateTime", ""))} for s in snaps[:20]]})

        return err("Unknown tool: " + t)
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

# Return success response / 성공 응답 반환
def ok(body): return {"statusCode": 200, "body": json.dumps(body, default=str)}
# Return error response / 오류 응답 반환
def err(msg): return {"statusCode": 400, "body": json.dumps({"error": msg})}
