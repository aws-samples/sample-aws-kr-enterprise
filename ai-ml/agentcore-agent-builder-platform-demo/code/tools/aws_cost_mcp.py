"""
AWS Cost Operations MCP Lambda - Cost Explorer, Pricing, Budgets
AWS 비용 운영 MCP 람다 - Cost Explorer, 요금 조회, 예산 관리
"""
import json
from datetime import datetime, timedelta
from cross_account import get_client, get_role_arn


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

    # Auto-detect tool from parameters if not specified / tool_name 미지정 시 파라미터로 도구 자동 감지
    if not t:
        if "dimension" in args: t = "get_dimension_values"
        elif "tag_key" in args: t = "get_tag_values"
        elif "forecast" in str(args).lower(): t = "get_cost_forecast"
        elif "compare" in str(args).lower() or "comparison" in str(args).lower(): t = "get_cost_and_usage_comparisons"
        elif "driver" in str(args).lower(): t = "get_cost_comparison_drivers"
        elif "start_date" in args or "granularity" in args: t = "get_cost_and_usage"
        elif "service_code" in args: t = "get_pricing"
        elif "budget" in str(args).lower(): t = "list_budgets"
        else: t = "get_today_date"

    try:
        # Get today's date and useful date references / 오늘 날짜 및 유용한 날짜 참조 반환
        if t == "get_today_date":
            now = datetime.utcnow()
            return ok({"today": now.strftime("%Y-%m-%d"), "month": now.strftime("%Y-%m"),
                "first_of_month": now.strftime("%Y-%m-01"),
                "last_month_start": (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m-01"),
                "last_month_end": now.strftime("%Y-%m-01")})

        # Get cost and usage breakdown by service / 서비스별 비용 및 사용량 조회
        elif t == "get_cost_and_usage":
            ce = get_client('ce', 'us-east-1', role_arn)
            now = datetime.utcnow()
            start = args.get("start_date", (now.replace(day=1) - timedelta(days=30)).strftime("%Y-%m-01"))
            end = args.get("end_date", now.strftime("%Y-%m-%d"))
            granularity = args.get("granularity", "MONTHLY")
            group_by_key = args.get("group_by", "SERVICE")
            metric = args.get("metric", "UnblendedCost")

            kwargs = {
                "TimePeriod": {"Start": start, "End": end},
                "Granularity": granularity,
                "Metrics": [metric],
                "GroupBy": [{"Type": "DIMENSION", "Key": group_by_key}],
            }
            if args.get("filter"):
                kwargs["Filter"] = args["filter"]

            # Query Cost Explorer for usage data, following NextPageToken so
            # total and breakdown cover the full period (not just page 1)
            # Cost Explorer 사용량 조회 — NextPageToken을 끝까지 순회해 전체 기간 합계/내역 확보
            results = []
            next_token = None
            while True:
                if next_token:
                    kwargs["NextPageToken"] = next_token
                resp = ce.get_cost_and_usage(**kwargs)
                for period in resp.get("ResultsByTime", []):
                    for group in period.get("Groups", []):
                        results.append({
                            "period": "{} ~ {}".format(period["TimePeriod"]["Start"], period["TimePeriod"]["End"]),
                            "key": group["Keys"][0],
                            "amount": float(group["Metrics"][metric]["Amount"]),
                            "unit": group["Metrics"][metric].get("Unit", "USD"),
                        })
                next_token = resp.get("NextPageToken")
                if not next_token:
                    break
            results.sort(key=lambda x: x["amount"], reverse=True)
            total = sum(r["amount"] for r in results)
            return ok({"period": "{} ~ {}".format(start, end), "granularity": granularity,
                "metric": metric, "total": round(total, 2), "breakdown": results[:30]})

        # Compare current vs previous month costs / 이번 달과 지난 달 비용 비교
        elif t == "get_cost_and_usage_comparisons":
            ce = get_client('ce', 'us-east-1', role_arn)
            now = datetime.utcnow()
            # Current is month-to-date; align previous to the SAME elapsed-days window
            # so we compare like periods (avoids a false "costs collapsed" reading early in the month).
            # 현재는 월초~오늘(MTD). 이전 달도 동일 경과일수 구간으로 맞춰 공정 비교(월초 착시 방지).
            cur_start_dt = now.replace(day=1)
            cur_end_dt = now  # CE End is exclusive → covers day 1..yesterday
            days_elapsed = max(1, (cur_end_dt - cur_start_dt).days)
            prev_start_dt = (cur_start_dt - timedelta(days=1)).replace(day=1)
            prev_end_dt = prev_start_dt + timedelta(days=days_elapsed)
            cur_start = cur_start_dt.strftime("%Y-%m-%d")
            cur_end = cur_end_dt.strftime("%Y-%m-%d")
            prev_start = prev_start_dt.strftime("%Y-%m-%d")
            prev_end = prev_end_dt.strftime("%Y-%m-%d")

            def get_costs(start, end):
                costs = {}
                next_token = None
                while True:
                    kw = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "MONTHLY",
                        "Metrics": ["UnblendedCost"], "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]}
                    if next_token:
                        kw["NextPageToken"] = next_token
                    resp = ce.get_cost_and_usage(**kw)
                    for p in resp.get("ResultsByTime", []):
                        for g in p.get("Groups", []):
                            svc = g["Keys"][0]
                            costs[svc] = costs.get(svc, 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
                    next_token = resp.get("NextPageToken")
                    if not next_token:
                        break
                return costs

            prev_costs = get_costs(prev_start, prev_end)
            cur_costs = get_costs(cur_start, cur_end)
            all_services = set(list(prev_costs.keys()) + list(cur_costs.keys()))
            comparison = []
            for svc in all_services:
                prev = prev_costs.get(svc, 0)
                cur = cur_costs.get(svc, 0)
                diff = cur - prev
                pct = (diff / prev * 100) if prev > 0 else (100 if cur > 0 else 0)
                if abs(diff) > 0.01:
                    comparison.append({"service": svc, "previous": round(prev, 2),
                        "current": round(cur, 2), "difference": round(diff, 2),
                        "percentChange": round(pct, 1)})
            comparison.sort(key=lambda x: abs(x["difference"]), reverse=True)
            return ok({"previousPeriod": "{} ~ {}".format(prev_start, prev_end),
                "currentPeriod": "{} ~ {}".format(cur_start, cur_end),
                "note": "Month-to-date ({} days) vs the same first-{} days of the previous month.".format(days_elapsed, days_elapsed),
                "previousTotal": round(sum(prev_costs.values()), 2),
                "currentTotal": round(sum(cur_costs.values()), 2),
                "comparison": comparison[:20]})

        # Identify top cost change drivers between months / 월간 비용 변동 주요 원인 분석
        elif t == "get_cost_comparison_drivers":
            ce = get_client('ce', 'us-east-1', role_arn)
            now = datetime.utcnow()
            # Align previous to the same month-to-date window as current (partial vs partial).
            # 이전 달도 현재와 동일한 경과일수(MTD) 구간으로 맞춰 비교.
            cur_start_dt = now.replace(day=1)
            cur_end_dt = now
            days_elapsed = max(1, (cur_end_dt - cur_start_dt).days)
            prev_start_dt = (cur_start_dt - timedelta(days=1)).replace(day=1)
            prev_end_dt = prev_start_dt + timedelta(days=days_elapsed)
            cur_start = cur_start_dt.strftime("%Y-%m-%d")
            cur_end = cur_end_dt.strftime("%Y-%m-%d")
            prev_start = prev_start_dt.strftime("%Y-%m-%d")
            prev_end = prev_end_dt.strftime("%Y-%m-%d")

            def get_detailed(start, end):
                costs = {}
                next_token = None
                while True:
                    kw = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "MONTHLY",
                        "Metrics": ["UnblendedCost"],
                        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}]}
                    if next_token:
                        kw["NextPageToken"] = next_token
                    resp = ce.get_cost_and_usage(**kw)
                    for p in resp.get("ResultsByTime", []):
                        for g in p.get("Groups", []):
                            key = " / ".join(g["Keys"])
                            costs[key] = costs.get(key, 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
                    next_token = resp.get("NextPageToken")
                    if not next_token:
                        break
                return costs

            prev = get_detailed(prev_start, prev_end)
            cur = get_detailed(cur_start, cur_end)
            all_keys = set(list(prev.keys()) + list(cur.keys()))
            drivers = []
            for k in all_keys:
                diff = cur.get(k, 0) - prev.get(k, 0)
                if abs(diff) > 0.1:
                    drivers.append({"driver": k, "previous": round(prev.get(k, 0), 2),
                        "current": round(cur.get(k, 0), 2), "impact": round(diff, 2)})
            drivers.sort(key=lambda x: abs(x["impact"]), reverse=True)
            return ok({"topDrivers": drivers[:10],
                "note": "Month-to-date ({} days) vs the same first-{} days of the previous month.".format(days_elapsed, days_elapsed),
                "period": "{} ~ {} vs {} ~ {}".format(prev_start, prev_end, cur_start, cur_end)})

        # Forecast future costs / 향후 비용 예측
        elif t == "get_cost_forecast":
            ce = get_client('ce', 'us-east-1', role_arn)
            now = datetime.utcnow()
            start = now.strftime("%Y-%m-%d")
            # Forecast to end of next month
            next_month = now.replace(day=28) + timedelta(days=4)
            end = (next_month.replace(day=28) + timedelta(days=4)).replace(day=1).strftime("%Y-%m-01")
            granularity = args.get("granularity", "MONTHLY")
            # Get cost forecast from Cost Explorer / Cost Explorer에서 비용 예측 조회
            resp = ce.get_cost_forecast(
                TimePeriod={"Start": start, "End": end}, Metric="UNBLENDED_COST",
                Granularity=granularity)
            forecasts = [{"period": "{} ~ {}".format(f["TimePeriod"]["Start"], f["TimePeriod"]["End"]),
                "mean": round(float(f.get("MeanValue", 0)), 2),
                "low": round(float(f.get("PredictionIntervalLowerBound", 0)), 2),
                "high": round(float(f.get("PredictionIntervalUpperBound", 0)), 2)}
                for f in resp.get("ForecastResultsByTime", [])]
            return ok({"total": round(float(resp.get("Total", {}).get("Amount", 0)), 2),
                "unit": resp.get("Total", {}).get("Unit", "USD"),
                "forecasts": forecasts})

        # Get available dimension values (e.g., SERVICE, REGION) / 사용 가능한 차원 값 조회 (예: SERVICE, REGION)
        elif t == "get_dimension_values":
            ce = get_client('ce', 'us-east-1', role_arn)
            now = datetime.utcnow()
            start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
            end = now.strftime("%Y-%m-%d")
            dimension = args.get("dimension", "SERVICE")
            resp = ce.get_dimension_values(
                TimePeriod={"Start": start, "End": end}, Dimension=dimension)
            values = [v["Value"] for v in resp.get("DimensionValues", [])]
            return ok({"dimension": dimension, "values": values[:50], "count": len(values)})

        # Get tag values for cost allocation / 비용 할당용 태그 값 조회
        elif t == "get_tag_values":
            ce = get_client('ce', 'us-east-1', role_arn)
            now = datetime.utcnow()
            start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
            end = now.strftime("%Y-%m-%d")
            tag_key = args.get("tag_key", "")
            resp = ce.get_tags(TimePeriod={"Start": start, "End": end}, TagKey=tag_key if tag_key else None)
            return ok({"tagKey": tag_key, "values": resp.get("Tags", [])[:50]})

        # Get AWS service pricing information / AWS 서비스 요금 정보 조회
        elif t == "get_pricing":
            pricing = get_client('pricing', 'us-east-1', role_arn)
            service_code = args.get("service_code", "AmazonEC2")
            filters = args.get("filters", [])
            kwargs = {"ServiceCode": service_code, "MaxResults": 10}
            if filters:
                kwargs["Filters"] = filters
            # Query AWS Pricing API / AWS Pricing API 조회
            resp = pricing.get_products(**kwargs)
            products = []
            for p in resp.get("PriceList", []):
                prod = json.loads(p) if isinstance(p, str) else p
                attrs = prod.get("product", {}).get("attributes", {})
                terms = prod.get("terms", {})
                products.append({"description": attrs.get("instanceType", attrs.get("usagetype", "")),
                    "region": attrs.get("location", ""), "os": attrs.get("operatingSystem", ""),
                    "attributes": {k: v for k, v in list(attrs.items())[:10]}})
            return ok({"serviceCode": service_code, "products": products})

        # List AWS Budgets with spend info / AWS 예산 목록 및 지출 정보 조회
        elif t == "list_budgets":
            budgets = get_client('budgets', 'us-east-1', role_arn)
            # Get account ID for Budgets API / Budgets API용 계정 ID 조회
            account_id = get_client('sts', 'us-east-1', role_arn).get_caller_identity()['Account']
            resp = budgets.describe_budgets(AccountId=account_id)
            budget_list = [{"name": b["BudgetName"], "type": b["BudgetType"],
                "limit": "{} {}".format(b["BudgetLimit"]["Amount"], b["BudgetLimit"]["Unit"]),
                "actualSpend": b.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", "0"),
                "forecastedSpend": b.get("CalculatedSpend", {}).get("ForecastedSpend", {}).get("Amount", "0")}
                for b in resp.get("Budgets", [])]
            return ok({"budgets": budget_list})

        return {"statusCode": 400, "body": json.dumps({"error": "Unknown tool: " + t})}

    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


# Return success response / 성공 응답 반환
def ok(body):
    return {"statusCode": 200, "body": json.dumps(body, default=str)}
