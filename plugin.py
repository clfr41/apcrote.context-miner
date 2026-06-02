"""上下文挖掘器 (ContextMiner) — 从聊天历史中按主题提取相关信息。

两种使用方式:
  Tool: context_mine — Planner 自动调用，结果直接返回
  Command: /ctx query --hours N --limit N — 用户手动触发

LLM 后端:
  - 内置: MaiBot ctx.llm.generate()
  - 自定义: OpenAI 兼容 API（配置 [custom_api] 启用）
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def peel_envelope(data: Any, max_depth: int = 4) -> Any:
    """递归脱掉 {'success': True, 'result': {...}} SDK 信封。

    照搬 diary_plugin 实现: 先检查 success+result 键存在且不为 None,
    再递归解开,最高兼容 4 层嵌套。兼容 SDK 自动解包后返回 list 的情况。
    """
    for _ in range(max_depth):
        if not isinstance(data, dict):
            return data
        if "result" not in data or "success" not in data:
            return data
        inner = data["result"]
        if inner is None:
            return data
        data = inner
    return data


def safe_timestamp(ts: Any) -> str:
    """安全地将时间戳转为可读格式。"""
    try:
        t = datetime.fromtimestamp(float(ts))
        return t.strftime("%m/%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return "??:??"


def _safe_int(value: Any, default: int) -> int:
    """将值安全转换为 int，失败返回默认值。"""
    if value is None:
        return default
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════
# 配置模型
# ═══════════════════════════════════════════════════════════════

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(default=False, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本号")


class ExtractConfig(PluginConfigBase):
    __ui_label__ = "提取设置"
    __ui_icon__ = "search"
    __ui_order__ = 1
    model: str = Field(
        default="replyer",
        description="提取用的 LLM 模型。可选: planner / utils / replyer"
    )
    default_hours: int = Field(
        default=24,
        description="默认回溯时长（小时），命令/Tool 未指定时使用"
    )
    max_context_chars: int = Field(
        default=12000,
        description="送入 LLM 的最大字符数，超过时从最新消息向前截断"
    )
    default_limit: int = Field(
        default=10,
        description="LLM 最多提炼的信息条数"
    )


class CustomApiConfig(PluginConfigBase):
    __ui_label__ = "自定义 API"
    __ui_icon__ = "globe"
    __ui_order__ = 2
    enabled: bool = Field(
        default=False,
        description="启用自定义 OpenAI 兼容 API（替代内置 LLM）"
    )
    api_url: str = Field(
        default="",
        description="API 端点，如 https://api.openai.com/v1"
    )
    api_key: str = Field(
        default="",
        description="API 密钥（明文存储，请勿分享配置文件）"
    )
    model: str = Field(
        default="",
        description="自定义 API 使用的模型名，留空则不指定"
    )


class ContextMinerConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)
    custom_api: CustomApiConfig = Field(default_factory=CustomApiConfig)


# ═══════════════════════════════════════════════════════════════
# 插件主类
# ═══════════════════════════════════════════════════════════════

class ContextMinerPlugin(MaiBotPlugin):
    config_model = ContextMinerConfig

    # ── 生命周期 ─────────────────────────────────────────────

    async def on_load(self) -> None:
        self.ctx.logger.info("[ContextMiner] 插件已加载")

    async def on_unload(self) -> None:
        self.ctx.logger.info("[ContextMiner] 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version

    # ── 步骤①: 获取聊天历史（照搬 diary_plugin 参数）─────────

    async def _fetch_history(
        self,
        stream_id: str,
        hours: int,
    ) -> list[dict]:
        """获取当前会话最近 N 小时内的全部聊天消息。

        采用 diary_plugin 的参数策略:
          - limit=0  → 不限条数，时间范围内全取
          - limit_mode="earliest" → 从最早开始
          - filter_mai=False       → 不滤 bot 消息
          - filter_command=False   → 不滤命令消息
        """
        end_ts = time.time()
        start_ts = end_ts - hours * 3600

        try:
            raw = await self.ctx.message.get_by_time_in_chat(
                stream_id,
                start_time=str(start_ts),
                end_time=str(end_ts),
                limit=0,
                limit_mode="earliest",
                filter_mai=False,
                filter_command=False,
            )
        except Exception as e:
            self.ctx.logger.error(f"[ContextMiner] 获取聊天历史失败: {e}")
            return []

        raw = peel_envelope(raw)
        if not isinstance(raw, list):
            self.ctx.logger.warning(
                f"[ContextMiner] 聊天历史返回格式异常: {type(raw)}"
            )
            return []

        # 反转 → 时间升序
        messages = [m for m in raw if isinstance(m, dict)]
        messages.reverse()
        return messages

    # ── 步骤①A: 格式化聊天历史为文本 ──────────────────────────

    def _format_history(self, messages: list[dict], max_chars: int) -> str:
        """将消息列表格式化为 LLM 可读的文本。超长时从旧消息截断。"""
        if not messages:
            return "（无聊天记录）"

        lines: list[str] = []
        total = 0
        for msg in messages:
            sender = self._extract_sender_name(msg)
            text = self._extract_message_text(msg)
            ts = safe_timestamp(msg.get("timestamp", 0))
            line = f"[{ts}] {sender}: {text}" if text else ""
            if line:
                lines.append(line)
                total += len(line)

        if total > max_chars:
            kept: list[str] = []
            kept_len = 0
            for line in reversed(lines):
                if kept_len + len(line) > max_chars:
                    break
                kept.insert(0, line)
                kept_len += len(line)
            lines = kept
            truncated = len(messages) - len(lines)
            if truncated > 0:
                lines.insert(0, f"（…省略最早 {truncated} 条消息…）")

        return "\n".join(lines)

    @staticmethod
    def _extract_sender_name(msg: dict) -> str:
        info = msg.get("message_info") or {}
        user = info.get("user_info") or {}
        return str(
            user.get("user_nickname")
            or user.get("user_id")
            or "未知"
        )

    @staticmethod
    def _extract_message_text(msg: dict) -> str:
        return str(msg.get("processed_plain_text") or msg.get("plain_text") or "").strip()

    # ── 步骤②: 构建提取 Prompt ─────────────────────────────────

    def _build_extraction_prompt(
        self,
        query: str,
        history_text: str,
        limit: int,
    ) -> str:
        """构建发送给 LLM 的提示词。limit 约束最多提炼条数。"""
        return f"""你是信息提取专家。请从以下聊天记录中，提取与指定主题相关的全部内容。

### 提取主题
{query}

### 聊天记录
{history_text}

### 要求
1. 找出与主题语义相关的所有消息及其上下文对话链
2. 修正提取内容中可能存在的错别字和语法错误
3. 按时间顺序整理，每一条保留发言人信息
4. **最多提炼 {limit} 条信息**，优先选择最重要的
5. 以结构化方式呈现：先给出总览摘要，再逐条列出提取内容
6. 如果完全没有相关内容，回复「无相关信息」
7. 不要编造聊天记录中不存在的信息
8. 不要添加额外解释或「根据聊天记录…」等前缀，直接输出提取结果"""

    # ── 步骤③A: 调用内置 LLM ──────────────────────────────────

    async def _call_builtin_llm(self, prompt: str, model: str) -> str:
        try:
            result = await self.ctx.llm.generate(
                prompt=prompt,
                model=model,
                temperature=0.3,
                max_tokens=4096,
            )
        except Exception as e:
            self.ctx.logger.error(f"[ContextMiner] 内置 LLM 调用异常: {e}")
            return ""

        result = peel_envelope(result)
        if isinstance(result, dict):
            if result.get("success"):
                return str(result.get("response", "")).strip()
            self.ctx.logger.error(
                f"[ContextMiner] 内置 LLM 返回失败: {result.get('error', '未知错误')}"
            )
            return ""
        return str(result).strip() if result else ""

    # ── 步骤③B: 调用自定义 API ────────────────────────────────

    async def _call_custom_api(self, prompt: str) -> str:
        """通过自定义 OpenAI 兼容 API 调用 LLM。使用标准库无外部依赖。"""
        import urllib.request

        cfg = self.config.custom_api
        url = cfg.api_url.rstrip("/") + "/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        }
        body_payload = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 4096,
        }
        if cfg.model:
            body_payload["model"] = cfg.model

        body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        try:
            loop = asyncio.get_running_loop()

            def _do_request() -> dict:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            data = await loop.run_in_executor(None, _do_request)
        except Exception as e:
            self.ctx.logger.error(f"[ContextMiner] 自定义 API 请求失败: {e}")
            return ""

        choices = data.get("choices") or []
        if not choices:
            self.ctx.logger.error(
                f"[ContextMiner] 自定义 API 返回无 choices: "
                f"{json.dumps(data, ensure_ascii=False)[:200]}"
            )
            return ""
        content = choices[0].get("message", {}).get("content", "")
        return str(content).strip()

    # ── 步骤③: 统一 LLM 调用入口 ──────────────────────────────

    async def _call_llm(self, prompt: str, model: str) -> str:
        if self.config.custom_api.enabled:
            return await self._call_custom_api(prompt)
        return await self._call_builtin_llm(prompt, model)

    # ── 步骤④: 核心提取逻辑 ─────────────────────────────────

    async def _extract(
        self,
        query: str,
        stream_id: str,
        hours: int,
        limit: int,
        model: str,
    ) -> tuple[bool, str]:
        """核心提取流程：拉历史 → 构建 prompt → 调 LLM → 返回。"""
        # ① 拉取聊天历史
        messages = await self._fetch_history(stream_id, hours)
        if not messages:
            return False, "未获取到任何聊天记录"

        # ①A 格式化为文本
        max_chars = self.config.extract.max_context_chars
        history_text = self._format_history(messages, max_chars)
        self.ctx.logger.info(
            f"[ContextMiner] 获取 {len(messages)} 条消息，"
            f"格式化后 {len(history_text)} 字符"
        )

        # ② 构建 prompt → ③ 调 LLM
        prompt = self._build_extraction_prompt(query, history_text, limit)
        self.ctx.logger.info(
            f"[ContextMiner] 调用 LLM: model={model}, limit={limit}"
        )
        start_ts = time.time()
        result = await self._call_llm(prompt, model)
        elapsed = time.time() - start_ts

        if not result:
            return False, "LLM 调用失败，请查看日志"

        self.ctx.logger.info(
            f"[ContextMiner] 提取完成，耗时 {elapsed:.1f}s，"
            f"结果长度 {len(result)} 字符"
        )
        return True, result

    # ═══════════════════════════════════════════════════════════
    # Tool: context_mine — Planner 自动调用
    # ═══════════════════════════════════════════════════════════

    @Tool(
        "context_mine",
        description=(
            "从当前会话的聊天历史中，按主题/关键词提取相关信息。"
            "会修正错别字和语法错误，结果直接返回供 Planner 使用。"
            "适用场景：当你需要了解群里之前讨论过的某个话题时调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="提取主题，用自然语言描述，例如「关于数据库选型的讨论」",
                required=True,
            ),
            ToolParameterInfo(
                name="hours",
                param_type=ToolParamType.NUMBER,
                description="回溯时长（小时），默认使用配置中的值",
                required=False,
            ),
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.NUMBER,
                description="最多提炼的信息条数，默认使用配置中的值",
                required=False,
            ),
        ],
    )
    async def tool_context_mine(
        self,
        query: str = "",
        hours: Any = None,
        limit: Any = None,
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict:
        """Tool 入口：Planner/LLM 自动调用。"""
        del kwargs

        if not stream_id:
            return {"name": "context_mine", "content": "", "error": "无法获取当前会话 ID"}

        if not query.strip():
            return {"name": "context_mine", "content": "", "error": "请提供要提取的主题描述"}

        cfg = self.config.extract
        h = _safe_int(hours, cfg.default_hours)
        lim = _safe_int(limit, cfg.default_limit)
        model = cfg.model

        ok, result = await self._extract(query, stream_id, h, lim, model)
        if not ok:
            return {"name": "context_mine", "content": "", "error": result}
        return {"name": "context_mine", "content": result}

    # ═══════════════════════════════════════════════════════════
    # Command: /ctx — 用户手动触发
    # ═══════════════════════════════════════════════════════════

    @Command(
        "ctx",
        description="从聊天历史中按主题提取相关信息",
        pattern=r"^/ctx\s+(?P<query>.+)$",
    )
    async def cmd_ctx(
        self,
        stream_id: str = "",
        matched_groups: dict = None,
        **kwargs: Any,
    ) -> tuple:
        """命令入口：/ctx query [--hours N] [--limit N] [--model NAME]"""
        del kwargs

        query = (matched_groups or {}).get("query", "").strip()
        if not query:
            return False, "用法: /ctx <查询> [--hours N] [--limit N]", True

        if not stream_id:
            return False, "无法获取当前会话 ID", True

        cfg = self.config.extract
        hours = cfg.default_hours
        limit = cfg.default_limit
        model = cfg.model

        # 解析内联参数: --hours / --limit / --model
        for flag, key in [("--hours", "hours"), ("--limit", "limit"), ("--model", "model")]:
            if flag in query:
                parts = query.split(flag, 1)
                if len(parts) == 2:
                    remainder = parts[1].strip()
                    val_str = remainder.split(" ", 1)[0].strip() if " " in remainder else remainder
                    if key == "hours":
                        hours = _safe_int(val_str, cfg.default_hours)
                    elif key == "limit":
                        limit = _safe_int(val_str, cfg.default_limit)
                    elif key == "model":
                        model = val_str
                    query = parts[0].strip() + (
                        (" " + remainder[len(val_str):].strip())
                        if remainder[len(val_str):].strip() else ""
                    )

        if not query:
            return False, "用法: /ctx <查询> [--hours N] [--limit N]", True

        self.ctx.logger.info(
            "[ContextMiner] 用户查询: '%s', hours=%d, limit=%d, model=%s",
            query, hours, limit, model,
        )

        await self.ctx.send.text(
            f"🔍 正在从最近 {hours} 小时聊天中提取「{query}」（上限 {limit} 条）…",
            stream_id,
        )

        ok, result = await self._extract(query, stream_id, hours, limit, model)
        if not ok:
            await self.ctx.send.text(f"❌ {result}", stream_id)
            return False, result, True

        await self.ctx.send.text(
            f"📋 上下文挖掘结果（上限 {limit} 条）\n\n{result}",
            stream_id,
        )
        return True, "提取完成", True


# ═══════════════════════════════════════════════════════════════
# 入口函数
# ═══════════════════════════════════════════════════════════════

def create_plugin() -> ContextMinerPlugin:
    return ContextMinerPlugin()
