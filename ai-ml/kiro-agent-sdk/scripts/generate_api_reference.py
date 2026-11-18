"""Generate a local API reference from the installed source checkout."""
from __future__ import annotations

import dataclasses
import inspect
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import kiro_agent_sdk as sdk  # noqa: E402
from kiro_agent_sdk import sync as sync_sdk  # noqa: E402


def signature(name: str, value: object) -> str:
    prefix = "async def " if inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value) else "def "
    return prefix + name + str(inspect.signature(value)) + ": ..."


def main() -> None:
    lines = [
        "# Python API 참조",
        "",
        "이 문서는 현재 소스의 공개 시그니처에서 생성됩니다. 동작과 제약은 [사용 가이드](guide.md),",
        "AgentCore HTTP 어댑터는 [AgentCore 가이드](agentcore.md)를 참고하세요.",
        "비동기 반복자는 `async for`로, 다른 비동기 함수는 `await`로 호출합니다.",
        "",
        "## 최상위 함수",
        "",
    ]
    for name in sdk.__all__:
        value = getattr(sdk, name)
        if not inspect.isfunction(value):
            continue
        lines.extend([f"### `{name}`", "", "```python", signature(name, value), "```", ""])
        doc = inspect.getdoc(value)
        if doc:
            lines.extend([doc.splitlines()[0], ""])

    lines.extend(["## 동기 세션 함수", "",
                  "`kiro_agent_sdk.sync`에서 가져오며 실행 중인 이벤트 루프에서는 사용할 수 없습니다.", "",
                  "```python"])
    for name in sync_sdk.__all__:
        lines.append(signature(name, getattr(sync_sdk, name)))
    lines.extend(["```", ""])

    lines.extend(["## `KiroSDKClient`", "",
                  "`KiroSDKClient(options=None, transport=None)`로 생성합니다. "
                  "`async with` 사용을 권장합니다.", ""])
    for name, value in sdk.KiroSDKClient.__dict__.items():
        if name.startswith("_"):
            continue
        if isinstance(value, property):
            lines.extend([f"### `{name}`", "", "읽기 전용 속성.", ""])
            doc = inspect.getdoc(value.fget)
            if doc:
                lines.extend([doc.splitlines()[0], ""])
        elif inspect.isfunction(value):
            lines.extend([f"### `{name}`", "", "```python", signature(name, value), "```", ""])
            doc = inspect.getdoc(value)
            if doc:
                lines.extend([doc.splitlines()[0], ""])

    for cls in (sdk.KiroSessionClient, sdk.AgentCorePrivateAccess):
        lines.extend([f"## `{cls.__name__}`", "", "```python", signature(cls.__name__, cls), "```", ""])
        for name, value in cls.__dict__.items():
            if not name.startswith("_") and inspect.isfunction(value):
                lines.extend([f"### `{name}`", "", "```python", signature(name, value), "```", ""])
                doc = inspect.getdoc(value)
                if doc:
                    lines.extend([doc.splitlines()[0], ""])

    lines.extend(["## 메시지와 데이터 클래스", "",
                  "다음은 공개 필드입니다. `<factory>`는 인스턴스별 기본값을 뜻합니다. "
                  "Kiro가 제공하지 않는 값은 비어 있거나 `None`일 수 있습니다.", ""])
    for name in sdk.__all__:
        value = getattr(sdk, name)
        if not inspect.isclass(value) or not dataclasses.is_dataclass(value):
            continue
        runtime_defaults = None
        lines.extend([f"### `{name}`", "", "| 필드 | 타입 | 기본값 |", "| --- | --- | --- |"])
        for item in dataclasses.fields(value):
            if item.default is not dataclasses.MISSING:
                default = repr(getattr(runtime_defaults, item.name)) if runtime_defaults is not None else repr(item.default)
            elif item.default_factory is not dataclasses.MISSING:
                default = "<factory>"
            else:
                default = "필수"
            lines.append(f"| `{item.name}` | `{item.type}` | `{default}` |")
        lines.append("")
        field_names = {item.name for item in dataclasses.fields(value)}
        properties = [key for key, item in value.__dict__.items()
                      if isinstance(item, property) and key not in field_names and not key.startswith("_")]
        if properties:
            lines.extend(["추가 속성: " + ", ".join(f"`{key}`" for key in properties), ""])

    lines.extend(["## 저장소와 전송", "",
                  "`SessionStore`의 필수 메서드는 `append`와 `load`입니다. "
                  "선택 메서드의 계약은 [세션 저장소](guide.md#세션-저장소)를 참고하세요.", ""])
    for cls in (sdk.SessionStore, sdk.InMemorySessionStore, sdk.SQLiteSessionStore,
                sdk.SQLiteSessionOwnership, sdk.DynamoDBSessionOwnership, sdk.SQLiteCreditLedger,
                sdk.DynamoDBCreditLedger, sdk.BackgroundTaskManager, sdk.SQLiteBackgroundRequestStore,
                sdk.DynamoDBBackgroundRequestStore, sdk.SQLiteBackgroundResultStore,
                sdk.DynamoDBBackgroundResultStore, sdk.InMemoryMcpOAuthStorage, sdk.Transport):
        lines.extend([f"### `{cls.__name__}`", "", "```python"])
        if cls in (sdk.BackgroundTaskManager, sdk.SQLiteBackgroundRequestStore, sdk.DynamoDBBackgroundRequestStore,
                   sdk.SQLiteBackgroundResultStore, sdk.DynamoDBBackgroundResultStore):
            lines.append(signature(cls.__name__, cls))
        for name, value in cls.__dict__.items():
            if not name.startswith("_") and inspect.isfunction(value):
                lines.append(signature(name, value))
        lines.extend(["```", ""])

    exceptions = [name for name in sdk.__all__ if inspect.isclass(getattr(sdk, name))
                  and issubclass(getattr(sdk, name), BaseException)]
    lines.extend(["## 오류", "",
                  ", ".join(f"`{name}`" for name in exceptions), "",
                  "의미와 확인할 사항은 [오류 처리](guide.md#오류-처리)를 참고하세요.", ""])
    lines.extend(["### `AgentCorePolicyApplyError`", "", "```python",
                  signature("AgentCorePolicyApplyError", sdk.AgentCorePolicyApplyError), "```", "",
                  "`applied_resources`, `failed_resource`, `verified_resources`, "
                  "`uncertain_resources`, `phase` 속성으로 중단 시점의 적용 상태를 확인합니다. "
                  "확인된 정책과 불확실한 쓰기의 구분은 [AgentCore 가이드](agentcore.md)를 참고하세요.", ""])

    lines.extend(["## `ToolAnnotations`", "", "```python",
                  "ToolAnnotations" + str(inspect.signature(sdk.ToolAnnotations)),
                  "```", "", "MCP 도구 힌트이며 권한 정책이 아닙니다.", ""])
    typed_dicts = [name for name in sdk.__all__ if hasattr(getattr(sdk, name), "__required_keys__")]
    lines.extend(["## 구조화된 딕셔너리 타입", "",
                  "타입 정의의 필드이며, 서버가 선택 필드를 실제로 보고한다는 보장은 없습니다.", ""])
    for name in typed_dicts:
        value = getattr(sdk, name)
        lines.extend([f"### `{name}`", "", "| 필드 | 타입 | 선언 |", "| --- | --- | --- |"])
        for key, annotation in value.__annotations__.items():
            annotation_text = getattr(annotation, "__forward_arg__", str(annotation))
            # PEP 655 markers come from typing on 3.11+ and typing_extensions
            # on 3.9/3.10; the documented type is the same on both.
            for prefix in ("typing_extensions.", "typing."):
                for marker in ("NotRequired", "Required"):
                    annotation_text = annotation_text.replace(prefix + marker + "[", marker + "[")
            required = "필수" if key in value.__required_keys__ else "선택"
            lines.append(f"| `{key}` | `{annotation_text}` | {required} |")
        lines.append("")

    documented = {name for name in sdk.__all__ if inspect.isfunction(getattr(sdk, name))}
    documented.update(name for name in sdk.__all__ if inspect.isclass(getattr(sdk, name))
                      and dataclasses.is_dataclass(getattr(sdk, name)))
    documented.update(exceptions)
    documented.update(typed_dicts)
    documented.update(("KiroSDKClient", "KiroSessionClient", "SessionStore", "InMemorySessionStore", "SQLiteSessionStore",
                       "SQLiteSessionOwnership", "DynamoDBSessionOwnership", "SQLiteCreditLedger",
                       "DynamoDBCreditLedger", "BackgroundTaskManager", "SQLiteBackgroundRequestStore",
                       "DynamoDBBackgroundRequestStore", "SQLiteBackgroundResultStore",
                       "DynamoDBBackgroundResultStore", "InMemoryMcpOAuthStorage",
                       "Transport", "ToolAnnotations"))
    remaining = [name for name in sdk.__all__ if name not in documented]
    lines.extend(["## 타입과 기타 이름", "",
                  ", ".join(f"`{name}`" for name in remaining), "",
                  "재생성: `python scripts/generate_api_reference.py`", ""])
    content = "\n".join(lines)
    path = ROOT / "docs" / "api-reference.md"
    if sys.argv[1:] == ["--check"]:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            raise SystemExit("API reference is out of date")
    elif sys.argv[1:]:
        raise SystemExit("usage: python scripts/generate_api_reference.py [--check]")
    else:
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
