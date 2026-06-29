from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.app import app


OUTPUT_PATH = Path("docs/apifox-openapi.json")


def main() -> None:
    spec = app.openapi()
    spec["info"] = {
        "title": "NL2SQL LangGraph API",
        "version": "1.0.0",
        "description": (
            "NL2SQL LangGraph API 文档。支持 JWT 登录态、Refresh Token "
            "轮换、自然语言转 SQL，以及会话上下文管理。"
        ),
    }
    spec["servers"] = [
        {
            "url": "http://localhost:8000",
            "description": "本地开发环境",
        }
    ]
    spec["tags"] = [
        {"name": "System", "description": "服务健康检查"},
        {"name": "Auth", "description": "登录态、JWT 与 Refresh Token 管理"},
        {"name": "NL2SQL", "description": "自然语言转 SQL"},
        {"name": "Conversations", "description": "会话与会话消息管理"},
    ]

    _normalize_security_scheme(spec)
    _ensure_error_schema(spec)
    _apply_operation_docs(spec)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _normalize_security_scheme(spec: dict[str, Any]) -> None:
    components = spec.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    if "HTTPBearer" in security_schemes:
        security_schemes.pop("HTTPBearer")
    security_schemes["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "登录或刷新接口返回的 access_token。",
    }

    for path_item in spec.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict) and operation.get("security") == [
                {"HTTPBearer": []}
            ]:
                operation["security"] = [{"BearerAuth": []}]


def _ensure_error_schema(spec: dict[str, Any]) -> None:
    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    schemas.setdefault(
        "HttpError",
        {
            "type": "object",
            "properties": {
                "detail": {
                    "description": "错误详情。认证、鉴权、404 与校验错误由 FastAPI 返回。",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "object"}},
                    ],
                }
            },
            "required": ["detail"],
            "title": "HttpError",
        },
    )
    schemas.setdefault(
        "InternalError",
        {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "example": False},
                "error": {"type": "string", "example": "Internal server error"},
                "detail": {"type": "string", "example": "exception message"},
            },
            "required": ["ok", "error", "detail"],
            "title": "InternalError",
        },
    )


def _apply_operation_docs(spec: dict[str, Any]) -> None:
    docs: dict[tuple[str, str], dict[str, Any]] = {
        ("/health", "get"): {
            "tag": "System",
            "operation_id": "health",
            "summary": "健康检查",
            "description": "检查 API 服务是否可用。",
            "response_example": {"ok": True, "service": "nl2sql-api"},
        },
        ("/api/auth/login", "post"): {
            "tag": "Auth",
            "operation_id": "authLogin",
            "summary": "用户名密码登录",
            "description": "按租户、用户名和密码登录，返回 access_token 与 refresh_token。",
            "request_example": {
                "tenant_id": "demo",
                "username": "alice",
                "password": "secret",
            },
            "response_example": _token_response_example(),
            "responses": {
                "401": _http_error_response(
                    "认证失败",
                    {"detail": "Invalid authentication credentials"},
                )
            },
        },
        ("/api/auth/refresh", "post"): {
            "tag": "Auth",
            "operation_id": "authRefresh",
            "summary": "刷新访问令牌",
            "description": "使用 refresh_token 轮换令牌，旧 refresh_token 会失效。",
            "request_example": {"refresh_token": "eyJhbGciOi...refresh"},
            "response_example": _token_response_example(),
            "responses": {
                "401": _http_error_response(
                    "Refresh Token 无效或已失效",
                    {"detail": "Invalid authentication credentials"},
                )
            },
        },
        ("/api/auth/me", "get"): {
            "tag": "Auth",
            "operation_id": "authMe",
            "summary": "获取当前登录用户",
            "description": "根据 Authorization Bearer access_token 返回当前用户身份。",
            "response_example": _user_response_example(),
            "responses": {
                "401": _http_error_response("未登录", {"detail": "Missing bearer token"})
            },
        },
        ("/api/auth/logout", "post"): {
            "tag": "Auth",
            "operation_id": "authLogout",
            "summary": "退出登录",
            "description": "传入 refresh_token 时撤销该 token；不传时返回成功。",
            "request_example": {"refresh_token": "eyJhbGciOi...refresh"},
            "response_example": {"ok": True},
            "responses": {
                "401": _http_error_response(
                    "Refresh Token 无效",
                    {"detail": "Invalid authentication credentials"},
                )
            },
        },
        ("/api/nl2sql", "post"): {
            "tag": "NL2SQL",
            "operation_id": "runNl2Sql",
            "summary": "自然语言转 SQL",
            "description": (
                "将自然语言问题转换为 SQL。tenant_id 可省略，省略时使用 token "
                "中的租户；如传入则必须与 token 租户一致。"
            ),
            "request_example": {
                "question": "按地区统计上月 GMV",
                "tenant_id": "demo",
                "execute": False,
                "timeout_ms": 10000,
                "max_limit": 1000,
                "max_validation_attempts": 2,
            },
            "response_example": _nl2sql_response_example(),
            "responses": _protected_responses(include_internal=True),
        },
        ("/api/conversations", "post"): {
            "tag": "Conversations",
            "operation_id": "createConversation",
            "summary": "创建会话",
            "description": (
                "创建一个会话。tenant_id 与 user_id 可省略，省略时使用 token "
                "中的身份；如传入则必须与 token 一致。"
            ),
            "request_example": {
                "tenant_id": "demo",
                "user_id": "user-001",
                "title": "上月 GMV 分析",
            },
            "response_example": _conversation_response_example(),
            "responses": _protected_responses(include_internal=True),
        },
        ("/api/conversations", "get"): {
            "tag": "Conversations",
            "operation_id": "listConversations",
            "summary": "查询会话列表",
            "description": (
                "查询当前用户的会话列表。tenant_id 与 user_id 可省略，"
                "省略时使用 token 身份。"
            ),
            "response_example": {"items": [_conversation_response_example()]},
            "responses": _protected_responses(include_internal=True),
        },
        ("/api/conversations/{conversation_id}", "get"): {
            "tag": "Conversations",
            "operation_id": "getConversation",
            "summary": "查询会话详情",
            "description": "按 conversation_id 查询当前用户可访问的会话。",
            "response_example": _conversation_response_example(),
            "responses": _protected_responses(
                include_not_found=True,
                include_internal=True,
            ),
        },
        ("/api/conversations/{conversation_id}", "patch"): {
            "tag": "Conversations",
            "operation_id": "updateConversation",
            "summary": "更新会话",
            "description": "更新会话标题或归档状态。",
            "request_example": {
                "tenant_id": "demo",
                "user_id": "user-001",
                "title": "上月 GMV 分析复盘",
                "archived": False,
            },
            "response_example": {
                **_conversation_response_example(),
                "title": "上月 GMV 分析复盘",
            },
            "responses": _protected_responses(
                include_not_found=True,
                include_internal=True,
            ),
        },
        ("/api/conversations/{conversation_id}/messages", "get"): {
            "tag": "Conversations",
            "operation_id": "listConversationMessages",
            "summary": "查询会话消息",
            "description": "查询指定会话的历史消息。",
            "response_example": {
                "items": [
                    {
                        "role": "user",
                        "content": "按地区统计上月 GMV",
                        "metadata": {"tenant_id": "demo"},
                    },
                    {
                        "role": "assistant",
                        "content": "SELECT region, SUM(gmv) AS gmv FROM orders GROUP BY region",
                        "metadata": {"ok": True},
                    },
                ]
            },
            "responses": _protected_responses(
                include_not_found=True,
                include_internal=True,
            ),
        },
        ("/api/conversations/{conversation_id}/messages", "post"): {
            "tag": "Conversations",
            "operation_id": "sendConversationMessage",
            "summary": "发送会话消息并生成 SQL",
            "description": (
                "向指定会话追加用户问题，并基于会话上下文执行 NL2SQL。"
            ),
            "request_example": {
                "question": "那按地区呢",
                "tenant_id": "demo",
                "user_id": "user-001",
                "execute": False,
                "timeout_ms": 10000,
                "max_limit": 1000,
                "max_validation_attempts": 2,
                "memory_history_limit": 8,
            },
            "response_example": _conversation_nl2sql_response_example(),
            "responses": _protected_responses(
                include_not_found=True,
                include_internal=True,
            ),
        },
    }

    for (path, method), metadata in docs.items():
        operation = spec.get("paths", {}).get(path, {}).get(method)
        if not operation:
            continue
        operation["tags"] = [metadata["tag"]]
        operation["operationId"] = metadata["operation_id"]
        operation["summary"] = metadata["summary"]
        operation["description"] = metadata["description"]
        _set_request_example(operation, metadata.get("request_example"))
        _set_success_response_example(operation, metadata.get("response_example"))
        _merge_responses(operation, metadata.get("responses", {}))
        _describe_parameters(operation)


def _set_request_example(
    operation: dict[str, Any],
    example: dict[str, Any] | None,
) -> None:
    if not example:
        return
    content = operation.get("requestBody", {}).get("content", {})
    media = content.get("application/json")
    if media is not None:
        media["example"] = example


def _set_success_response_example(
    operation: dict[str, Any],
    example: dict[str, Any] | None,
) -> None:
    if example is None:
        return
    responses = operation.setdefault("responses", {})
    success = responses.get("200")
    if not success:
        return
    content = success.setdefault("content", {}).setdefault("application/json", {})
    content["example"] = example


def _merge_responses(
    operation: dict[str, Any],
    responses: dict[str, dict[str, Any]],
) -> None:
    operation_responses = operation.setdefault("responses", {})
    for status_code, response in responses.items():
        operation_responses.setdefault(status_code, response)


def _describe_parameters(operation: dict[str, Any]) -> None:
    descriptions = {
        "conversation_id": "会话 ID。",
        "tenant_id": "租户 ID。可省略，省略时使用 access_token 中的租户。",
        "user_id": "用户 ID。可省略，省略时使用 access_token 中的用户。",
        "limit": "返回数量上限。",
        "include_archived": "是否包含已归档会话。",
    }
    examples = {
        "conversation_id": "conv_01HY...",
        "tenant_id": "demo",
        "user_id": "user-001",
        "limit": 20,
        "include_archived": False,
    }
    for parameter in operation.get("parameters", []):
        name = parameter.get("name")
        if name in descriptions:
            parameter["description"] = descriptions[name]
        if name in examples:
            parameter["example"] = examples[name]


def _protected_responses(
    *,
    include_not_found: bool = False,
    include_internal: bool = False,
) -> dict[str, dict[str, Any]]:
    responses = {
        "401": _http_error_response("未登录或 access_token 无效", {"detail": "Missing bearer token"}),
        "403": _http_error_response("身份不匹配", {"detail": "Tenant does not match token"}),
    }
    if include_not_found:
        responses["404"] = _http_error_response(
            "资源不存在",
            {"detail": "Conversation not found"},
        )
    if include_internal:
        responses["500"] = {
            "description": "服务内部错误",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/InternalError"},
                    "example": {
                        "ok": False,
                        "error": "Internal server error",
                        "detail": "exception message",
                    },
                }
            },
        }
    return responses


def _http_error_response(description: str, example: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/HttpError"},
                "example": example,
            }
        },
    }


def _user_response_example() -> dict[str, Any]:
    return {
        "tenant_id": "demo",
        "user_id": "user-001",
        "username": "alice",
        "roles": ["admin"],
    }


def _token_response_example() -> dict[str, Any]:
    return {
        "access_token": "eyJhbGciOi...access",
        "refresh_token": "eyJhbGciOi...refresh",
        "token_type": "bearer",
        "expires_in": 1800,
        "user": _user_response_example(),
    }


def _conversation_response_example() -> dict[str, Any]:
    return {
        "tenant_id": "demo",
        "conversation_id": "conv_01HY...",
        "user_id": "user-001",
        "title": "上月 GMV 分析",
        "archived": False,
        "created_at": "2026-06-26T10:00:00Z",
        "updated_at": "2026-06-26T10:00:00Z",
    }


def _nl2sql_response_example() -> dict[str, Any]:
    return {
        "ok": True,
        "question": "按地区统计上月 GMV",
        "tenant_id": "demo",
        "intent": {"metrics": ["gmv"], "dimensions": ["region"]},
        "sql": "SELECT region, SUM(gmv) AS gmv FROM orders GROUP BY region",
        "rows": [],
        "answer": "",
        "error": "",
        "trace": [],
    }


def _conversation_nl2sql_response_example() -> dict[str, Any]:
    response = {
        **_nl2sql_response_example(),
        "contextualized_question": "在上月 GMV 的基础上，按地区统计",
        "conversation_id": "conv_01HY...",
        "user_id": "user-001",
    }
    response["question"] = "那按地区呢"
    return response


if __name__ == "__main__":
    main()
