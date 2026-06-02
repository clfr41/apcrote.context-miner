# 上下文挖掘器

从当前会话的聊天历史中按主题提取相关信息。支持 Planner 自动调用（`context_mine` Tool）和用户手动 `/ctx` 命令。自带错别字修正、超长截断、自定义 API 后端。

## 安装

将 `apcrote.context-miner` 目录放入 MaiBot 的 `plugins/` 目录下。

## 配置

编辑 `config.toml`：

```toml
[plugin]
enabled = false
config_version = "1.0.0"

[extract]
model = "replyer"           # 提取用模型: planner / utils / replyer
default_hours = 24          # 默认回溯时长（小时）
max_context_chars = 12000   # 送入 LLM 的最大字符数，超出从旧消息截断
default_limit = 25          # 最多提炼的信息条数

[custom_api]
enabled = false             # 设为 true 使用外部 API 替代内置 LLM
api_url = ""                # 如 https://api.openai.com/v1
api_key = ""
model = ""
```

## 使用

### Tool: `context_mine`

Planner 自动调用，无需用户手动输入：

```
Planner → context_mine(query="数据库选型讨论", hours=48, limit=5)
       → {name: "context_mine", content: "1. 最终选定了 PostgreSQL…\n2. …"}
       → Planner 消化结果，继续后续流程
```

### Command: `/ctx`

用户手动触发：

| 命令 | 说明 |
|------|------|
| `/ctx 数据库选型` | 从最近 24h 提取相关讨论，最多 10 条 |
| `/ctx 部署方案 --hours 72 --limit 5` | 回溯 72h，最多提炼 5 条 |

回复示例：

```
📋 上下文挖掘结果（上限 5 条）

### 总览摘要
提取了与"数据库选型"相关的 3 条讨论...

1. [06/01 14:22] 张三: PostgreSQL 的 JSONB 支持更好
2. [06/01 14:25] 李四: 但 MySQL 8.0 的 JSON 也还行
3. [06/02 09:10] 张三: 那就 PG 吧，别纠结了
```

---

# ⚠️ 免责声明

## 在使用本插件之前，您必须完整阅读并同意以下全部条款。

### 一、插件内容过大，噎着概不负责

本插件干的事情概括起来三句话：拉聊天记录、丢给 LLM、返回结果。但为了实现这三句话，它做了：信使脱壳（四层递归防 None）、时间戳格式化（四种异常全捕获）、消息文本格式化（双字段 fallback + 超长截断反转算法）、LLM 双后端（内置 `ctx.llm.generate` + 自定义 OpenAI API `urllib` 线程池异步化）、命令内联参数解析（从自然语言查询中暴力拆解 `--hours` `--limit` `--model`）、Tool/Command 双入口共享核心管道。代码量 ≈ 400 行。若您感到"这不是就 `grep` + ChatGPT 么"——是，但**作者概不负责**。

### 二、与其他插件不兼容，崩了概不负责

1. **命令命名污染**：`/ctx` 占用了两条杠一个斜杠的珍贵命令前缀。若您的 Bot 同时安装了 `/ctx` 上下文清理插件而导致语义冲突——**作者概不负责**，`ctx` 既可以是 context 也可以是 clean-the-x，建议改名 `/clean-context`。

2. **Tool 名称冲突**：`context_mine` 这个 Tool 名注册到了 Planner 的 tool list 中。若其他插件也注册了名为 `context_mine` 的 Tool，会触发"同名 Tool 最后的覆盖前面的"这种静默灾难——**作者概不负责**，这是 MaiBot 的 Tool 注册机制问题。

3. **LLM 模型占用**：本插件每次调用会消耗一条内置 LLM 请求。若您的 Bot 正处在"和 47 个人同时聊天"的高峰期，而 Planner 疯狂调用 `context_mine` 导致队列爆炸——**作者概不负责**，请在 `config.toml` 里换一个轻量模型。

4. **自定义 API 崩溃**：自定义 API 使用的是 Python 标准库 `urllib.request` + `asyncio.run_in_executor`。若您的 API 提供商返回了非标准 JSON、HTTP 502、或者带 HTML 标签的错误页面——本插件的错误处理只兜住了三层，第四层会突破到 `self.ctx.logger.error` 然后静默返回空字符串，**作者概不负责**。

### 三、功能性免责

1. **聊天历史拿不到**：本插件调用 `ctx.message.get_by_time_in_chat` 获取历史。若 MaiBot 的消息仅存内存（未持久化到数据库），则只能获取开机后到现在的记录；跨重启的消息永远拿不到。若您因此发现"查 72 小时历史但只有 4 条"——**作者概不负责**，这是 MaiBot 的存储架构问题，本插件已经采用了和 diary_plugin 完全一致的参数（`limit=0, limit_mode=earliest, filter_mai=False, filter_command=False`），能拿多少全靠天。

2. **LLM 胡说八道**：提取内容由 LLM 生成。若 LLM 把两段不相关的聊天强行拼成一条惊天阴谋，或把张三的发言归到李四名下——**作者概不负责**，prompt 里已经写了"不要编造不存在的信息"，但 LLM 听不听是它的事。

3. **错别字修正过度**：本插件要求 LLM"修正错别字和语法错误"。若 LLM 把群友精心设计的方言、梗、缩写、抽象话全部修正为标准书面语，导致群友看到后惊呼"这他妈是谁说的我根本没这么文雅"——**作者概不负责**，建议把 `default_limit` 调小然后假装没看见。

4. **超长截断丢信息**：`max_context_chars=12000` 会在格式化后的历史文本超长时从最旧的消息开始丢弃。若因此丢失了关键上下文，导致 LLM 返回了"无相关信息"——**作者概不负责**，可以调大 `max_context_chars` 或者缩短 `default_hours`。

5. **Planner 消化不良**：Tool 返回的结构化文本由 Planner 自行消化。若 Planner 拿到"数据库选型确定为 PG"这条信息后仍然问出"所以你们到底用了什么数据库"——**作者概不负责**，这是 Planner 的理解问题。

### 四、宇宙级免责

本插件按"原样"（AS IS）提供。使用本插件即表示您同意：无论发生什么事——包括但不限于聊天历史丢失、LLM 胡说、错过关键讨论、被人吐槽"这不就是让 AI 再看一遍聊天记录吗"、因 `default_limit` 设太大而让 Planner 产生"我什么都知道了"的错觉——**均与作者无关**。

**如果您不同意以上任何一条，请不要启用本插件。继续使用即视为全部同意。**

---

## 许可证

本插件采用 GPL v3.0 或更高版本。
