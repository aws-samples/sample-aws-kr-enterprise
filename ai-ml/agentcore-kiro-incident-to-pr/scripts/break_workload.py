"""Demo scenario: a developer commits a wrong ALB health check path to the workload repository.

    python scripts/break_workload.py [--region ap-northeast-2]

The commit changes the target group health check from /health to /healthz, a path the application does not serve.
The pipeline deploys it, health checks get HTTP 404, both targets become unhealthy and the incident-to-pr-alb-unhealthy
alarm fires. The agents then analyze the incident and open a pull request that restores the path.
"""
import argparse

import boto3

REPOSITORY = "incident-to-pr-workload"
PATH = "lib/sample-app-stack.ts"
CORRECT = 'healthCheck: { path: "/health",'
WRONG = 'healthCheck: { path: "/healthz",'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default="ap-northeast-2", help="region of the demo stacks")
    args = parser.parse_args()
    codecommit = boto3.client("codecommit", region_name=args.region)
    head = codecommit.get_branch(repositoryName=REPOSITORY, branchName="main")["branch"]["commitId"]
    source = codecommit.get_file(repositoryName=REPOSITORY, commitSpecifier=head, filePath=PATH)["fileContent"].decode()
    if CORRECT not in source:
        raise SystemExit(f"{PATH} on main does not contain {CORRECT!r}; restore it before running the scenario")
    commit = codecommit.put_file(
        repositoryName=REPOSITORY, branchName="main", parentCommitId=head, filePath=PATH,
        fileContent=source.replace(CORRECT, WRONG).encode(), name="Demo Developer", email="developer@example.com",
        commitMessage="Use the standard /healthz health check path")["commitId"]
    print(f"Committed {commit[:8]} to main ({PATH}: health check /health -> /healthz). "
          "The pipeline deploys it in a few minutes.")


if __name__ == "__main__":
    main()
