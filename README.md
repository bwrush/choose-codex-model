# choose-codex-model

中文：一个面向 Codex 工作流的模型选择 Skill。它结合任务风险、质量门槛、候选池完整性和 CodexRadar 的公开指标，给出可解释的建议；它 does not automatically switch your active configuration.

English: A model-selection Skill for Codex workflows. It combines task risk, quality gates, candidate-pool completeness, and public CodexRadar metrics to produce explainable recommendations; it does not automatically switch your active configuration.

## Features / 功能

- Six-dimension risk assessment and quality-first filtering.
- Cost-first ranking: Radar IQ 10%, average-price ratio 70%, average duration 15%, and evidence 5%; Radar contributes 80% and task fit contributes 20%.
- A complete current Codex App model-picker list is required before an account-verified configuration can be ranked; when that list is unknown, default mode can still give a clearly labelled public Radar-only comparison from the known Codex model family, never a third-party Radar model.
- The Skill automatically uses model and reasoning-effort metadata already surfaced for the current task. It does not read, click, scrape, or infer the native model picker.
- Live Radar data may fall back only to a schema-validated local temporary cache no older than 24 hours.
- Strict mode, unknown configuration, and an unqualified current configuration retain their pause protections. A qualified short task is not interrupted merely because another qualified candidate scores higher.

## Requirements / 前提

- Codex and Python 3.9+.
- Network access when live CodexRadar data is desired.
- No third-party Python packages.

## Install / 安装

1. Back up an existing Skill with the same name if you already have one.
2. Copy this repository's `skill` directory into your Codex user skills directory and name the installed folder `choose-codex-model`.
3. Start a new Codex task and ask for a model choice, risk assessment, or use `$choose-codex-model` explicitly.

安装前请备份已有的同名 Skill。将仓库中的 `skill` 目录复制到 Codex 用户 Skill 目录，并把安装后的文件夹命名为 `choose-codex-model`。

## Use / 使用

At the start of an assessment, the Skill automatically passes the current task's surfaced model and reasoning effort as `runtime_current` when the host exposes them. It never invents an unknown Standard/Fast value, and it does not read, click, scrape, or infer the native model picker.

Obtain the complete current model-picker list from your Codex App when you want an account-verified configuration recommendation. Only that complete list may set `available_complete` to `true`. Official documentation can establish public model information, but it cannot establish your account availability.

开始评估时，如果宿主已暴露本任务的模型和推理档位，Skill 会自动作为 `runtime_current` 传入；它不会猜测 Standard/Fast，也不会读取、点击、抓取或推断原生模型选择器。

如需“账户已验证”的配置推荐，请从当前 Codex App 提供完整模型选择器列表。只有这份完整列表才能设置 `available_complete=true`；官方文档不能证明某个模型在你的账户中可用。

Example prepare input:

```json
{
  "action": "prepare",
  "risk_dimensions": {
    "reasoning": 1,
    "impact": 1,
    "reversibility": 1,
    "scope": 1,
    "uncertainty": 1,
    "verification": 1
  },
  "current": null,
  "runtime_current": {"model": "gpt-5.6-terra", "effort": "xhigh"},
  "runtime_current_source": "codex_task_metadata",
  "available": null,
  "available_complete": false
}
```

Pipe that JSON to `skill/scripts/recommend.py` with `action=prepare`. If a valid non-strict result has `available_complete=false`, then pass the complete prepared object back with `action=radar_only`. It returns a public Radar-only recommendation only from the known Codex model family, never a third-party Radar model. When a user explicitly enables `luna_quality_baseline`, a public Luna max metric may raise the quality floor; this does not establish Luna account availability. Its output explicitly says that account availability is unverified, and it does not automatically switch anything. Strict mode and genuinely insufficient Radar data still return no new recommendation.

## Configuration / 配置

All per-task controls are explicit input values:

| Field | Default | Meaning |
|---|---|---|
| `strict` | `false` | Pause when policy requires a stricter safety decision. |
| `pause_on_change` | `false` | Request pause protection for unknown or unqualified current configurations. |
| `allow_fast` | `false` | Allow Fast only with an explicit latency preference. |
| `luna_max_fast_preference` | `false` | Opt in to a Luna-only max Fast preference. |
| `luna_quality_baseline` | `false` | Opt in to Luna max Standard IQ as an additional quality floor. |
| `comparison_speed` | `standard` | Compare all candidates at Standard speed. |
| `task_horizon` | `short` | Use long only for an explicitly long or at-least-60-minute workflow. |
| `notify_on_large_savings` | `true` | For a long task, allow one non-blocking verified price-gap notice. |

The two Luna controls are opt-in. They are not defaults of this public package.

## Verify / 验证

Run these commands from the repository root:

```powershell
python -B -X utf8 -c "from pathlib import Path; compile(Path('skill/scripts/recommend.py').read_text(encoding='utf-8'), 'skill/scripts/recommend.py', 'exec')"
python -B -X utf8 -m unittest discover -s skill\tests -p "test_*.py" -v
```

If your Codex installation supplies a separate Skill validator, it may be run as an additional local check; this repository does not depend on or bundle that validator.

## Data, privacy, and limitations / 数据、隐私与限制

CodexRadar is a third-party data source. Its average price is a comparison proxy, not a token-savings claim or account bill. Rankings, prices, and account availability can change. The package contains no credentials, model-picker snapshot, or Radar response. Runtime cache data stays in the operating system temporary directory and is used only when it passes schema and age validation.

CodexRadar 是第三方数据源。其平均价格只用于相对比较，不代表 token 节省或账户账单；排名、价格和账户可用性都可能变化。

This is an independent community project. It is not affiliated with, endorsed by, or supported by OpenAI or CodexRadar.

## Contributing / 贡献

Please reproduce behavior changes with a failing `unittest` first, keep candidate-availability claims verifiable, and avoid adding personal defaults or captured account data.

## License / 许可证

[MIT](LICENSE)
