"""模型池核心：配置加载 + 断路器 + 自动切换。

失败语义（任一命中即判"本模型本次不可用"，立即切下一个）：
- 连接错误 / 读超时（socket 层）
- HTTP 401/403/404/429/5xx（鉴权、路由、限流、上游故障）
- HTTP 200 但 choices 为空 / 正文为空（空返回，项目历史实测过这类假成功）

断路器：一个模型连续失败 PMH_BREAK_THRESHOLD 次进入冷却（PMH_COOLDOWN_SECONDS，
默认 300s），冷却期内直接跳过；冷却到期自动放行真实请求自愈。
这样 GLM-5.3-Flash 这类"目录在列但当前极慢"的模型不会拖垮主链，
也不会被永久打入冷宫。

环境变量：
- PMH_CONFIG      模型池配置路径（默认 <root>/config/modelhub.json）
- PMH_ROLES       角色设定路径（默认 <root>/config/roles.json）
- PMH_TIMEOUT     每次请求超时秒数（默认 60；非法/非正回退默认）
- PMH_BREAK_THRESHOLD   连续失败 N 次熔断（默认 3）
- PMH_COOLDOWN_SECONDS  熔断冷却秒数（默认 300）
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

try:  # python-dotenv 是项目既有依赖；缺失时退化为只读进程环境变量
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

from .ledger import append_call_event, append_switch_event


class ConfigError(RuntimeError):
    """配置缺失或损坏——要求显式报错，绝不静默失败。"""


class GatewayError(RuntimeError):
    """模型网关层错误（含 HTTP 状态与响应片段）。"""

    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status

    @property
    def reason(self) -> str:
        """从错误文本与状态码推断切换原因（写台账用）。"""
        text = str(self)
        status = self.http_status
        if status == 401 or status == 403:
            return "auth_error"
        if status == 404:
            return "model_not_found"
        if status == 429:
            return "rate_limited"
        if status is not None and 500 <= status < 600:
            return "upstream_5xx"
        low = text.lower()
        if "超时" in text or "timeout" in low:
            return "timeout"
        if "连接" in text or "connection" in low:
            return "connection_error"
        if "空" in text:
            return "empty_response"
        return "gateway_error"


class ModelPoolExhaustedError(RuntimeError):
    """池内所有模型都对本次请求失败。"""


class ModelEntry:
    """池内一个模型条目。

    upstream_model：发往上游的模型名，默认等于 name。
    用于不同端点出现同名模型时给池内一个唯一对外名
    （如商汤 deepseek-v4-flash 与 AMD DeepSeek-V4-Flash）。
    """

    def __init__(self, spec: dict[str, Any]):
        self.name: str = spec["name"]
        self.base_url: str = spec["base_url"].rstrip("/")
        self.api_key: str = spec["api_key"]
        self.upstream_model: str = str(spec.get("upstream_model") or self.name)
        self.priority: int = int(spec.get("priority", 99))
        self.family: str = spec.get("family", "")
        self.source: str = spec.get("source", "")
        self.notes: str = spec.get("notes", "")
        self.enabled: bool = bool(spec.get("enabled", True))
        self.timeout_override: int | None = spec.get("timeout_seconds")
        # 断路器状态
        self.consecutive_failures: int = 0
        self.opened_at: float | None = None
        self.last_error: str | None = None
        self.last_success_ts: float | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "upstream_model": self.upstream_model,
            "priority": self.priority,
            "family": self.family,
            "source": self.source,
            "enabled": self.enabled,
        }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}（可从 config/*.example 复制后填写）")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"配置文件不可读（存在但无法读取）：{path}：{e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"配置文件损坏（不是合法 JSON）：{path}：第 {e.lineno} 行 {e.msg}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是 JSON 对象：{path}")
    return data


def _resolve_placeholders(obj: Any, env_map: dict[str, str]) -> Any:
    """递归解析 "${ENV_NAME}" 形式的环境变量占位符。

    密钥不写死在 config 文件里（config 会进 git），引用 .env 导出的环境变量。
    """
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("${") and s.endswith("}"):
            env_name = s[2:-1].strip()
            val = env_map.get(env_name, "")
            if not val:
                raise ConfigError(
                    f"环境变量 {env_name} 未设置或为空（配置里引用了 ${{{env_name}}}）。"
                    f"请检查 .env 是否存在并被 load_dotenv 加载。"
                )
            return val
        return obj
    if isinstance(obj, list):
        return [_resolve_placeholders(x, env_map) for x in obj]
    if isinstance(obj, dict):
        return {k: _resolve_placeholders(v, env_map) for k, v in obj.items()}
    return obj


def _timeout_seconds() -> float:
    raw = (os.getenv("PMH_TIMEOUT") or "").strip()
    try:
        v = float(raw) if raw else 60.0
    except ValueError:
        v = 60.0
    return v if v > 0 else 60.0


def _break_threshold() -> int:
    raw = (os.getenv("PMH_BREAK_THRESHOLD") or "").strip()
    try:
        v = int(raw) if raw else 3
    except ValueError:
        v = 3
    return max(1, v)


def _cooldown_seconds() -> float:
    raw = (os.getenv("PMH_COOLDOWN_SECONDS") or "").strip()
    try:
        v = float(raw) if raw else 300.0
    except ValueError:
        v = 300.0
    return v if v > 0 else 300.0


class ModelHub:
    """线程安全的模型池网关。"""

    def __init__(self, config_path: Path | None = None):
        self._lock = threading.RLock()
        # 显式加载项目根 .env（绝对路径，不依赖 CWD）：${PM_API_KEY_*} 占位符靠它解析
        if load_dotenv is not None:
            load_dotenv(_project_root() / ".env")
        self._config_path = config_path or Path(
            os.getenv("PMH_CONFIG", str(_project_root() / "config" / "modelhub.json"))
        )
        self._entries: list[ModelEntry] = []
        self._mtime: float = 0.0
        self._reload_if_needed(force=True)

    # ---- 配置 ----
    def _reload_if_needed(self, force: bool = False) -> None:
        with self._lock:
            if not self._config_path.exists():
                raise ConfigError(
                    f"配置文件不存在：{self._config_path}（可从 config/*.example 复制后填写）"
                )
            try:
                mtime = self._config_path.stat().st_mtime
            except OSError as e:
                raise ConfigError(f"模型池配置不可访问：{self._config_path}：{e}") from e
            if force or mtime != self._mtime:
                data = _load_json(self._config_path)
                # 解析 ${ENV} 占位符（密钥不落 config 明文）；环境缺失时 ConfigError 显式报错
                data = _resolve_placeholders(data, dict(os.environ))
                pool = data.get("pool")
                if not isinstance(pool, list) or not pool:
                    raise ConfigError(f"模型池配置缺少非空的 pool 数组：{self._config_path}")
                entries = []
                for i, spec in enumerate(pool):
                    if not isinstance(spec, dict):
                        raise ConfigError(f"pool[{i}] 不是对象：{self._config_path}")
                    for req in ("name", "base_url", "api_key"):
                        if not spec.get(req):
                            raise ConfigError(f"pool[{i}] 缺少必填字段 {req}：{self._config_path}")
                    entries.append(ModelEntry(spec))
                if not any(e.enabled for e in entries):
                    raise ConfigError("模型池里没有任何 enabled=true 的模型")
                self._entries = entries
                self._mtime = mtime

    def reload(self) -> None:
        self._reload_if_needed(force=True)

    # ---- 视图 ----
    def list_models(self) -> list[dict[str, Any]]:
        with self._lock:
            self._reload_if_needed()
            return [e.to_public_dict() for e in self._entries]

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._reload_if_needed()
            now = time.time()
            cooldown = _cooldown_seconds()
            models = []
            for e in self._entries:
                cooling = e.opened_at is not None and (now - e.opened_at) < cooldown
                models.append(
                    {
                        **e.to_public_dict(),
                        "circuit": "open(cooling)" if cooling else "closed",
                        "consecutive_failures": e.consecutive_failures,
                        "last_error": e.last_error,
                        "cooling_remaining_s": (
                            int(cooldown - (now - e.opened_at)) if cooling else 0
                        ),
                    }
                )
            return {
                "models": models,
                "break_threshold": _break_threshold(),
                "cooldown_seconds": cooldown,
            }

    def _ordered_enabled(self) -> list[ModelEntry]:
        now = time.time()
        cooldown = _cooldown_seconds()
        avail = [
            e
            for e in self._entries
            if e.enabled and (e.opened_at is None or (now - e.opened_at) >= cooldown)
        ]
        avail.sort(key=lambda e: (e.priority, e.name))
        return avail

    # ---- 断路器 ----
    def _mark_success(self, e: ModelEntry) -> None:
        with self._lock:
            e.consecutive_failures = 0
            e.opened_at = None
            e.last_error = None
            e.last_success_ts = time.time()

    def _mark_failure(self, e: ModelEntry, err: str) -> None:
        with self._lock:
            e.consecutive_failures += 1
            e.last_error = err[:300]
            if e.consecutive_failures >= _break_threshold():
                # 连败达到阈值即（重新）进入冷却
                e.opened_at = time.time()

    # ---- HTTP ----
    @staticmethod
    def _http_post_json(
        url: str, api_key: str, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
                status = r.status
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise GatewayError(f"HTTP {e.code}: {detail}", http_status=e.code) from e
        except urllib.error.URLError as e:
            # URLError 包裹的 socket.timeout 也要归为超时
            reason = str(getattr(e, "reason", "") or e)
            if "timed out" in reason.lower() or "timeout" in reason.lower():
                raise GatewayError(f"读超时（>{int(timeout)}s）") from e
            raise GatewayError(f"连接失败: {reason}") from e
        except TimeoutError as e:
            raise GatewayError(f"读超时（>{int(timeout)}s）") from e
        except OSError as e:
            raise GatewayError(f"网络错误: {e}") from e
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise GatewayError(f"响应不是 JSON（status={status}）: {body[:120]}") from e
        return data

    @staticmethod
    def _validate_chat_response(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GatewayError("响应 choices 为空（假成功）")
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            raise GatewayError("响应正文为空（choices 存在但 content 空）")
        return content

    # ---- 主入口 ----
    # 瞬时错误原地重试：上游偶发 401（实测存在）/429/5xx 在同一模型上立即重试
    # 往往就能成功（2026-09-21 发布测试实测：401 后 2s 重试即 200）。
    # 切换是给“持续性故障”用的；瞬时抖动先原地重试一次，减少无谓切换。
    TRANSIENT_RETRY_CODES = (401, 402, 403, 429, 500, 502, 503, 504)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        agent: str | None = None,
        role: str | None = None,
        stream: bool = False,
        **params: Any,
    ) -> dict[str, Any]:
        """带自动切换的对话补全。

        model=None：按 priority 顺序走主备链，逐个尝试；
        model="x"：先试 x，失败仍按 priority 切换兜底（池内其他模型）。
        stream=True：当前版本返回 400 提示（流式在 roadmap，台账会如实记录）。
        """
        if stream:
            raise GatewayError(
                "ModelHub 当前版本仅支持非流式（stream=true 不支持）；"
                "请传 stream=false 或省略 stream 参数",
                http_status=400,
            )

        self._reload_if_needed()
        request_id = uuid.uuid4().hex[:12]
        t0 = time.time()
        candidates = self._ordered_enabled()
        if not candidates:
            raise ModelPoolExhaustedError(
                "模型池当前没有可用模型（全部处于熔断冷却或禁用）。"
                "请稍后重试或检查 config/modelhub.json 与各端点状态。"
            )

        if model is not None:
            head = [e for e in candidates if e.name == model]
            rest = [e for e in candidates if e.name != model]
            candidates = head + rest
            if not head:
                append_switch_event(
                    request_id=request_id,
                    from_model="(specified)",
                    to_model=candidates[0].name if candidates else "(none)",
                    reason="model_not_in_pool",
                    detail=f"请求的模型 {model} 不在池内，回落到 priority 主备链",
                    failover_index=0,
                )

        timeout = _timeout_seconds()
        last_entry: ModelEntry | None = None
        last_err: GatewayError | None = None
        failovers = 0

        for idx, entry in enumerate(candidates):
            if idx > 0:
                failovers = idx
                append_switch_event(
                    request_id=request_id,
                    from_model=(last_entry.name if last_entry else "?"),
                    to_model=entry.name,
                    reason=(last_err.reason if last_err else "unknown"),
                    detail=str(last_err)[:300] if last_err else None,
                    failover_index=failovers,
                )
            last_entry = entry
            etimeout = float(entry.timeout_override or timeout)
            url = entry.base_url + "/chat/completions"
            payload: dict[str, Any] = {"model": entry.upstream_model, "messages": messages}
            payload.update(params)
            try:
                data = self._http_post_json(url, entry.api_key, payload, etimeout)
                content = self._validate_chat_response(data)
                self._mark_success(entry)
                latency_ms = int((time.time() - t0) * 1000)
                usage = data.get("usage") or {}
                append_call_event(
                    request_id=request_id,
                    model=entry.name,
                    endpoint=entry.base_url,
                    success=True,
                    latency_ms=latency_ms,
                    attempts=idx + 1,
                    failovers=failovers,
                    http_status=200,
                    error_type=None,
                    error_msg=None,
                    agent=agent,
                    role=role,
                    content_chars=len(content),
                )
                return {
                    "content": content,
                    "model": entry.name,
                    "endpoint": entry.base_url,
                    "request_id": request_id,
                    "failovers": failovers,
                    "latency_ms": latency_ms,
                    "usage": usage,
                    "raw_finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
                }
            except GatewayError as e:
                last_err = e
                self._mark_failure(entry, f"{type(e).__name__}: {e}")
                # D-009：瞬时错误（偶发 401/429/5xx/连接抖动）先原地重试一次再切换，
                # 实测这类错误 2s 后重试即成功；持续性故障仍由切换链兑底。
                transient = (
                    e.http_status in self.TRANSIENT_RETRY_CODES
                    or e.reason in ("timeout", "connection_error")
                )
                if transient:
                    try:
                        time.sleep(1.5)
                        data = self._http_post_json(url, entry.api_key, payload, etimeout)
                        content = self._validate_chat_response(data)
                        self._mark_success(entry)
                        latency_ms = int((time.time() - t0) * 1000)
                        usage = data.get("usage") or {}
                        append_call_event(
                            request_id=request_id,
                            model=entry.name,
                            endpoint=entry.base_url,
                            success=True,
                            latency_ms=latency_ms,
                            attempts=idx + 2,
                            failovers=failovers,
                            http_status=200,
                            error_type=None,
                            error_msg=None,
                            agent=agent,
                            role=role,
                            content_chars=len(content),
                        )
                        return {
                            "content": content,
                            "model": entry.name,
                            "endpoint": entry.base_url,
                            "request_id": request_id,
                            "failovers": failovers,
                            "latency_ms": latency_ms,
                            "usage": usage,
                            "raw_finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
                            "transient_retried": True,
                        }
                    except GatewayError:
                        # 重试仍失败 → 走切换链（_mark_failure 已计过一次失败，
                        # 这里补计第二次，让连败计数如实反映两次真实失败）
                        self._mark_failure(entry, f"retry-failed: {last_err}")
                continue

        latency_ms = int((time.time() - t0) * 1000)
        append_call_event(
            request_id=request_id,
            model=(last_entry.name if last_entry else "(none)"),
            endpoint=(last_entry.base_url if last_entry else "?"),
            success=False,
            latency_ms=latency_ms,
            attempts=len(candidates),
            failovers=max(0, len(candidates) - 1),
            http_status=(last_err.http_status if last_err else None),
            error_type=type(last_err).__name__ if last_err else "ModelPoolExhaustedError",
            error_msg=str(last_err) if last_err else "all models failed",
            agent=agent,
            role=role,
            content_chars=None,
        )
        tried = ", ".join(e.name for e in candidates)
        raise ModelPoolExhaustedError(
            f"池内 {len(candidates)} 个模型全部失败（尝试顺序：{tried}）。最后错误：{last_err}"
        )

    def list_routes(self) -> list[str]:
        return ["/v1/models", "/v1/chat/completions", "/v1/ledger", "/v1/pool/status", "/v1/agents"]
