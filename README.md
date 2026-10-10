# Agentic Software Factory · 935.V1

2026 OAIC 智能体软件工厂黑客松获奖作品，版本 **935.V1**。

本项目是面向 ARC-Bench 的需求驱动软件生成智能体：读取 `requirements.yaml`，规划应用架构和实现单元，执行「实现 → 测试 → 审查 → 修复」循环，交付可运行的 Web 应用，并记录需求追踪、运行事件与 Git 历史。

本仓库公开本次获奖版本的完整源码、应用模板与测试。本页统一介绍该版本的设计、运行方式和验证方法。

- [比赛官网](https://create.gosim.org/factory26/)
- [官方规则](https://create.gosim.org/factory26/rules)
- [比赛平台 ARC-Bench](https://arc-bench.com/competition)

## 核心能力

- 需求树解析、共享约束提取、依赖排序与实现单元拆分。
- 稳定提示词前缀与源码上下文缓存，模型调用预算、超时与重试控制。
- 逐单元生成代码、Playwright 验收测试、审查和回归修复。
- 多阶段任务记忆：保存既有需求、规划与验收测试，对继承应用建立回归保护。
- ARC-Bench 事件、追踪数据和 Git 提交记录；异常时输出降级交付诊断。

## 935.V1 设计

架构规划设置单次 600 秒、总计 1500 秒的时间预算；超时后降低推理强度重试，并记录已产生的推理和回答长度，便于定位规划失败。

多阶段任务在生成项目的 `evogit/` 目录保存当前验收测试、先前阶段测试、需求历史与规划。继承已有应用时，先运行先前阶段测试，将原本通过的测试文件作为回归保护，贯穿单元实现、审查、修复和最终验证。规划同时读取已有文件、路由、API、数据键和需求，保留已经实现的功能。

每个实现单元执行「实现 → 测试 → 审查」，修复只有在满足回归检查时才被保留。回滚修复时恢复原文件修改时间，使源码上下文顺序保持稳定，支持提示词前缀缓存复用。模型预算、超时重试、构建检查和异常诊断共同支撑交付过程。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `main.py` | ARC-Bench 兼容的命令行入口，版本 935.V1 |
| `capability_agent.py`, `new_task_driver.py` | 智能体编排与任务生命周期 |
| `generated_application.py`, `tdd_loop.py` | 架构规划、代码生成、测试、审查与修复 |
| `requirement_context.py`, `unit_adaptation.py`, `context_select.py` | 需求约束、实现单元与源码上下文 |
| `stage_memory.py` | 多阶段需求、规划和回归测试记忆 |
| `model_transport.py`, `budget_policy.py` | 模型传输、重试与预算策略 |
| `acceptance.py`, `acceptance_assets/`, `app_runtime.py` | 验收测试生成、浏览器辅助与应用运行检查 |
| `arcbench-agent-runtime/` | 本地 Python 运行时包：事件、需求追踪与 Git 操作 |
| `template/` | 应用初始模板：React/Vite 前端与 Node.js 后端宿主 |
| `tests/` | 智能体的本地单元和回归测试 |

## 环境与安装

建议在与比赛沙箱一致的 Linux 环境运行。需要 Python 3.10+、Git、Node.js 和 npm；前端模板使用 Vite 7，建议 Node.js 22.12+。浏览器验收需要 Playwright 和可运行的 Chromium，运行过程中可能下载依赖。Windows 可运行本地 Python 测试，建议启用 UTF-8。

从仓库根目录安装 Python 依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell 激活方式为 `.\.venv\Scripts\Activate.ps1`。

## 模型配置

程序读取以下环境变量；模型网关需支持 OpenAI 兼容的 Chat Completions 接口：

| 变量 | 说明 |
| --- | --- |
| `OPENAI_API_KEY` | 网关凭据，使用自己的凭据或比赛平台注入的凭据 |
| `OPENAI_BASE_URL` | 模型网关 API 地址 |
| `MODEL` | 网关提供的模型标识 |
| `ARCBENCH_TASK_DIR` | 可选，需求目录；默认 `requirements` |
| `ARCBENCH_OUTPUT_DIR` | 可选，应用输出目录；建议显式指定独立目录 |
| `ARCBENCH_TASK_TYPE` | 可选，任务类型；默认 `web` |

Linux 示例（替换占位内容）：

```bash
export OPENAI_API_KEY="<your-api-key>"
export OPENAI_BASE_URL="<your-model-gateway-url>"
export MODEL="<your-model-id>"
```

PowerShell 使用 `$env:OPENAI_API_KEY = "<your-api-key>"` 等对应写法。程序不自动加载 `.env` 文件。

## 运行

准备需求目录，其中包含平台格式的 `requirements.yaml`，然后从仓库根目录运行：

```bash
python main.py /path/to/requirements --output-dir /path/to/output-workspace --type web
```

也可以直接传入 `requirements.yaml` 文件路径。输出目录必须与本仓库分开；平台运行时也可以通过上述环境变量提供目录。

智能体会调用模型生成应用、安装前后端依赖、构建并测试。完整运行需要有效模型网关、相应额度和浏览器环境。本仓库不包含私有网关凭据、比赛需求数据或官方评测结果。
