"""Workload repository and pipeline tools: read the GitOps history, and open one fix pull request.

The workload's IaC and application code live in a CodeCommit repository whose main branch is deployed by
CodePipeline. Agents read commits, diffs, files and pipeline runs. The fixer agent changes files only on a new
branch under FIX_BRANCH_PREFIX and opens a pull request into main; a person reviews and merges it, and the pipeline
deploys it. The IAM role enforces the same limits (PutFile on fix branches only, no merge or push).
"""
import difflib
import json
import os
import re
import uuid
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"))
REPOSITORY = os.environ.get("WORKLOAD_REPOSITORY", "incident-to-pr-workload")
PIPELINE = os.environ.get("WORKLOAD_PIPELINE", "incident-to-pr-workload")
BRANCH_PREFIX = os.environ.get("FIX_BRANCH_PREFIX", "incident/")
MAIN = "main"
# Set by the platform for one fixer session; the tool writes its result there and opens at most one pull request.
FIX_DIR_ENV = "INCIDENT_FIX_DIR"
REPORT_ID_ENV = "INCIDENT_REPORT_ID"
MAX_FILE_BYTES = 200_000
MAX_DIFF_CHARS = 20_000
MAX_FILES = 5
SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,255}")
# Fix pull requests change the IaC and application code only; build and tool configuration (package.json, lockfiles,
# cdk.json, bin/) stays out of them, so an agent change cannot alter what runs when the workload is built.
CHANGEABLE = ("lib/", "app/")

codecommit = boto3.client("codecommit", region_name=REGION)
codepipeline = boto3.client("codepipeline", region_name=REGION)


def _head(ref):
    return codecommit.get_branch(repositoryName=REPOSITORY, branchName=ref)["branch"]["commitId"]


def _text(blob_id):
    data = codecommit.get_blob(repositoryName=REPOSITORY, blobId=blob_id)["content"]
    return data.decode("utf-8", errors="replace")


def _valid_path(path):
    return (isinstance(path, str) and bool(SAFE_PATH.fullmatch(path)) and ".." not in path.split("/")
            and not path.startswith("/") and "//" not in path)


# ========================================================================
# Read
# ========================================================================

def list_recent_commits(limit: int = 10, branch: str = MAIN) -> str:
    """List the latest commits of a workload repository branch (newest first): ID, message, author and time."""
    try:
        commit_id, commits = _head(branch), []
        while commit_id and len(commits) < max(1, min(limit, 30)):
            commit = codecommit.get_commit(repositoryName=REPOSITORY, commitId=commit_id)["commit"]
            commits.append({"commit_id": commit_id, "message": commit.get("message", "").strip()[:500],
                            "author": commit.get("author", {}).get("name", ""),
                            "date": commit.get("committer", {}).get("date", "")})
            parents = commit.get("parents") or []
            commit_id = parents[0] if parents else None
        return json.dumps({"repository": REPOSITORY, "branch": branch, "commits": commits}, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


def get_commit_changes(commit_id: str) -> str:
    """Show what one commit changed compared with its parent: changed files and a unified diff of each."""
    try:
        commit = codecommit.get_commit(repositoryName=REPOSITORY, commitId=commit_id)["commit"]
        parents = commit.get("parents") or []
        kwargs = {"repositoryName": REPOSITORY, "afterCommitSpecifier": commit_id}
        if parents:
            kwargs["beforeCommitSpecifier"] = parents[0]
        changes, used = [], 0
        for page in codecommit.get_paginator("get_differences").paginate(**kwargs):
            for difference in page.get("differences", []):
                before, after = difference.get("beforeBlob") or {}, difference.get("afterBlob") or {}
                path = after.get("path") or before.get("path", "")
                old = _text(before["blobId"]).splitlines(keepends=True) if before.get("blobId") else []
                new = _text(after["blobId"]).splitlines(keepends=True) if after.get("blobId") else []
                diff = "".join(difflib.unified_diff(old, new, f"a/{path}", f"b/{path}"))
                diff = diff[:max(0, MAX_DIFF_CHARS - used)]
                used += len(diff)
                changes.append({"path": path, "change": difference.get("changeType", ""), "diff": diff})
        return json.dumps({"commit_id": commit_id, "message": commit.get("message", "").strip()[:500],
                           "parent": parents[0] if parents else None, "changes": changes}, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


def read_repository_file(path: str, ref: str = MAIN) -> str:
    """Read a text file of the workload repository at a branch or commit (default main)."""
    if not _valid_path(path):
        return "Error: use a plain relative path such as lib/sample-app-stack.ts"
    try:
        file = codecommit.get_file(repositoryName=REPOSITORY, commitSpecifier=ref, filePath=path)
        if len(file["fileContent"]) > MAX_FILE_BYTES:
            return f"Error: {path} is larger than {MAX_FILE_BYTES} bytes"
        return json.dumps({"path": path, "commit_id": file["commitId"],
                           "content": file["fileContent"].decode("utf-8", errors="replace")}, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


def list_repository_files(folder: str = "", ref: str = MAIN) -> str:
    """List files and folders of the workload repository (one level) at a branch or commit."""
    if folder and not _valid_path(folder):
        return "Error: use a plain relative folder such as lib"
    try:
        result = codecommit.get_folder(repositoryName=REPOSITORY, commitSpecifier=ref, folderPath="/" + folder)
        return json.dumps({"folder": folder or "/", "files": [f["relativePath"] for f in result.get("files", [])],
                           "folders": [f["relativePath"] for f in result.get("subFolders", [])]}, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


def list_pipeline_executions(limit: int = 5) -> str:
    """Latest runs of the workload deployment pipeline: status, start and end time, and the deployed commit."""
    try:
        runs = codepipeline.list_pipeline_executions(pipelineName=PIPELINE, maxResults=max(1, min(limit, 20)))
        return json.dumps({"pipeline": PIPELINE, "executions": [
            {"execution_id": run["pipelineExecutionId"], "status": run["status"],
             "started": str(run.get("startTime", "")), "updated": str(run.get("lastUpdateTime", "")),
             "commits": [{"commit_id": s.get("revisionId"), "summary": (s.get("revisionSummary") or "")[:300]}
                         for s in run.get("sourceRevisions", [])]}
            for run in runs.get("pipelineExecutionSummaries", [])]}, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


# ========================================================================
# Write: one fix pull request per fixer session
# ========================================================================

def _record(directory, result):
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    return result


def open_fix_pull_request(title: str, description: str, changes: list[dict[str, str]]) -> str:
    """Open the pull request that fixes the incident. Call it once, after reading the files you change.

    Args:
        title: Short pull request title in Korean
        description: Pull request body in Korean: root cause, evidence, what the change does and how to verify it
        changes: Files to change, each {"path": "lib/...", "content": "the complete new file content"}.
            Only existing files under lib/ or app/ can be changed, at most 5, and each must differ from main.
    """
    directory = Path(os.environ.get(FIX_DIR_ENV, ""))
    if not os.environ.get(FIX_DIR_ENV) or not directory.is_dir():
        return "Error: pull requests are opened only by the fixer agent of an RCA report"
    if (directory / "result.json").exists():
        return "Error: this report already has a pull request; it cannot open another one"
    if not isinstance(title, str) or not title.strip() or not isinstance(description, str) or not description.strip():
        return "Error: title and description are required"
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_FILES:
        return f"Error: change between 1 and {MAX_FILES} files"
    try:
        base = _head(MAIN)
        files = []
        for change in changes:
            path, content = (change.get("path"), change.get("content")) if isinstance(change, dict) else (None, None)
            if not _valid_path(path) or not isinstance(content, str) or not content.strip():
                return f"Error: invalid change for {path!r}"
            if not path.startswith(CHANGEABLE):
                return f"Error: {path} is not under lib/ or app/; fix pull requests change only IaC and application code"
            data = content.encode("utf-8")
            if len(data) > MAX_FILE_BYTES:
                return f"Error: {path} is larger than {MAX_FILE_BYTES} bytes"
            current = codecommit.get_file(repositoryName=REPOSITORY, commitSpecifier=base, filePath=path)
            if current["fileContent"] == data:
                return f"Error: {path} is unchanged"
            files.append((path, data))
        if len({path for path, _ in files}) != len(files):
            return "Error: each file can appear only once"
    except codecommit.exceptions.FileDoesNotExistException:
        return "Error: only existing files can be changed"
    except Exception as e:
        return f"Error: {e}"
    report_id = re.sub(r"[^A-Za-z0-9-]", "", os.environ.get(REPORT_ID_ENV, ""))[:40] or uuid.uuid4().hex[:12]
    branch = f"{BRANCH_PREFIX}fix-{report_id}"
    result = {"branch": branch, "base_commit": base, "files": [path for path, _ in files], "title": title.strip()[:150]}
    try:
        codecommit.create_branch(repositoryName=REPOSITORY, branchName=branch, commitId=base)
        parent = base
        for path, data in files:
            parent = codecommit.put_file(repositoryName=REPOSITORY, branchName=branch, fileContent=data,
                                         filePath=path, parentCommitId=parent, commitMessage=result["title"],
                                         name="Kiro Fixer", email="kiro-fixer@example.invalid")["commitId"]
        pull_request = codecommit.create_pull_request(
            title=result["title"], description=description.strip()[:10000],
            targets=[{"repositoryName": REPOSITORY, "sourceReference": branch, "destinationReference": MAIN}],
        )["pullRequest"]
    except Exception as e:
        _record(directory, {**result, "status": "FAILED", "error": f"{type(e).__name__}: {e}"[:500]})
        return f"Error: the pull request was not opened: {e}"
    _record(directory, {**result, "status": "OPEN", "pull_request_id": pull_request["pullRequestId"],
                        "source_commit": parent})
    return json.dumps({"pull_request_id": pull_request["pullRequestId"], "branch": branch,
                       "files": result["files"]}, ensure_ascii=False)


def read_fix_result(directory):
    """The pull request the tool opened in a fixer session, or None; the model's own text is never used for this."""
    try:
        result = json.loads((Path(directory) / "result.json").read_text())
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) else None


ANCESTRY_LIMIT = 50


def _includes(revision, commit_id):
    """Whether revision is commit_id or a later commit built on it (CodeCommit has no ancestry API; walks parents)."""
    pending, seen = [revision], set()
    while pending and len(seen) < ANCESTRY_LIMIT:
        current = pending.pop()
        if current == commit_id:
            return True
        if not current or current in seen:
            continue
        seen.add(current)
        pending.extend(codecommit.get_commit(repositoryName=REPOSITORY, commitId=current)["commit"].get("parents") or [])
    return False


def deployment(pull_request):
    """(state, run): OPEN, CLOSED, MERGED, or DEPLOYED with the succeeded pipeline run whose source includes the merge.

    A run counts only if the commit it built is the merge commit or a descendant of it; ancestry is only walked for
    runs that started after the merge commit was made. A later rollback does not change a DEPLOYED result.
    """
    pr = codecommit.get_pull_request(pullRequestId=str(pull_request["pull_request_id"]))["pullRequest"]
    target = (pr.get("pullRequestTargets") or [{}])[0]
    merge = target.get("mergeMetadata") or {}
    if not merge.get("isMerged"):
        return ("OPEN" if pr.get("pullRequestStatus") == "OPEN" else "CLOSED"), None
    merged = merge.get("mergeCommitId") or target.get("sourceCommit")
    merged_at = int(codecommit.get_commit(repositoryName=REPOSITORY, commitId=merged)["commit"]["committer"]["date"].split()[0])
    runs = codepipeline.list_pipeline_executions(pipelineName=PIPELINE, maxResults=20)
    for run in runs.get("pipelineExecutionSummaries", []):
        if run.get("status") != "Succeeded":
            continue
        recent = run["startTime"].timestamp() >= merged_at - 300
        for source in run.get("sourceRevisions", []):
            revision = source.get("revisionId")
            if revision == merged or (revision and recent and _includes(revision, merged)):
                return "DEPLOYED", {"execution_id": run.get("pipelineExecutionId", ""), "revision": revision}
    return "MERGED", None


def pull_request_state(pull_request):
    """OPEN, CLOSED, MERGED, DEPLOYED, or UNKNOWN when CodeCommit or CodePipeline cannot be read."""
    try:
        return deployment(pull_request)[0]
    except Exception:
        return "UNKNOWN"


REPOSITORY_TOOLS = [list_recent_commits, get_commit_changes, read_repository_file, list_repository_files,
                    list_pipeline_executions]
CHANGE_TOOLS = [open_fix_pull_request]
