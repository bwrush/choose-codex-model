---
name: choose-codex-model
description: Use when starting or finishing a Codex task, choosing or comparing a Codex model, reasoning effort, or Standard/Fast mode, assessing task difficulty or risk, using CodexRadar, handling strict mode, or responding to "下个任务用什么模型", "评估难度", and "模型选择".
---

# Choose Codex Model

Evaluate the current task before work. While the overall task remains unfinished, evaluate only an explicit next task or concrete unblocking action. When the overall task is complete and no explicit next task exists, suggest and assess exactly one grounded, adjacent, executable next task without executing it.

## Terminal-output gate

Before ending any turn, route it to exactly one terminal state. `PREFLIGHT_STOP_KNOWN`, `PREFLIGHT_STOP_AMBIGUOUS`, `HANDOFF_NEXT_DEFINED`, and `HANDOFF_NEXT_SUGGESTED` must all end with the footer of exactly two lines. If the overall task is complete and no explicit next task exists, proactively suggest exactly one grounded, adjacent, executable next task, assess it, and use `HANDOFF_NEXT_SUGGESTED` without executing it.

## Non-negotiable policy

- Apply quality gates before cost or latency ranking; only qualified candidates proceed to ranking.
- Among qualified candidates, rank cost-first with Radar weights of IQ 10%, price 70%, minutes 15%, and evidence 5%.
- Score price by actual average-price ratio to the cheapest qualified candidate, not by candidate-pool percentile rank.
- Rank with CodexRadar data at 80% and candidate-level task fit at 20%.
- Official OpenAI documentation may verify model existence and supported reasoning effort(s), but does not prove current account or Codex app availability.
- Never change the active Codex configuration automatically.
- Treat Radar `average_price_usd` as a comparable price proxy, not as a token-usage metric.
- Never invent an unavailable configuration, missing Radar value, or compatibility result.
- An account-verified configuration recommendation may come only from a successful `rank` result and a qualified candidate.
- A `radar_only` recommendation is allowed only for a qualified public-Radar comparison in default mode; it must never claim account availability, replace the complete-picker gate, or change the active configuration.
- Default to `Standard`. Recommend `Fast` only when the current task, or the latest unoverridden explicit preference, prioritizes latency and accepts extra usage.
- A later `Standard` or cost-priority instruction revokes earlier `Fast` authorization.
- Task-local `Fast` authorization expires when that task ends.
- Only an explicitly persistent user preference may carry across tasks.
- `luna_max_fast_preference` defaults to `false`. Enable it only when the user explicitly enables it for this assessment or establishes it as an explicitly persistent user preference.
- When it is enabled, Luna is limited to `max · Fast`; other models remain `Standard` by default. A task-local explicit latency authorization may still opt a non-Luna candidate into `Fast`.
- Disable `luna_max_fast_preference` when the user explicitly revokes it; otherwise pass `false`.
- `luna_quality_baseline` defaults to `false`. Enable it only when the user explicitly enables it for this assessment or establishes it as an explicitly persistent user preference.
- When it is enabled, Luna max Standard IQ is the minimum quality floor; a higher risk-based quality floor still applies.
- Compare every candidate at Standard speed; do not use Fast-mode speed to rank candidates. Luna max Fast remains an output preference and does not change the comparison baseline.
- When recommending `Fast`, state that it comes from the user's explicit latency preference unless live Radar data directly distinguishes the speed modes.

## Risk assessment

Score each dimension from 0 to 4: 0 none, 1 low, 2 medium, 3 high, 4 extreme.

| Dimension | Weight | Judge |
|---|---:|---|
| reasoning | 25 | multi-step reasoning, architecture, research synthesis, hard debugging |
| impact | 20 | rework, data loss, production incidents, or decision loss |
| reversibility | 15 | writes, deployments, deletion, messages, or external-system effects |
| scope | 15 | number of files, components, repositories, systems, and dependencies |
| uncertainty | 15 | ambiguity, unfamiliar domains, missing information, and exploration |
| verification | 10 | lack of clear tests, objective answers, or reliable acceptance checks |

Send the six levels to the script. Set `force_l4=true` for production changes, irreversible deletion, secrets/security, or other high-impact external actions.

In strict mode, if the task description plausibly spans two or more risk levels, use `PREFLIGHT_STOP_AMBIGUOUS`; do not hide ambiguity inside a guessed score.

## Assessment workflow

For the current task before execution, or for the explicit or suggested next task at handoff:

1. For every assessment, first read the host's surfaced task metadata. When it exposes a model and reasoning effort, pass them as `runtime_current` and `runtime_current_source=codex_task_metadata`. This automatic read uses only metadata already exposed to the task; do not ask the user for permission first. Do not read, click, scrape, or infer the native model picker.
2. Keep runtime metadata separate from a complete current configuration. Do not synthesize a `speed` value: when a full `{model, effort, speed}` configuration is not separately surfaced or supplied by the user, pass `current=null`. `runtime_current` never proves account availability and never substitutes for `current`.
3. Obtain the currently available `{model, effort}` combinations only from a complete current Codex app model-picker list surfaced by the host or explicitly supplied by the user. Set `available_complete=true` only for that complete list. For an absent, partial, remembered, manually supplied, or history-inferred list, set `available_complete=false`. Set `available_complete=false` for a manually supplied list. Set `available_complete=false` for a list inferred from history. Use `$openai-docs` only to establish model existence: official documentation does not prove account availability and cannot set `available_complete=true`. When the complete app list is unknown, pass `available=null` while keeping `available_complete=false`; never pass `null` for the boolean marker.

When `available_complete=false`, report `available model list is incomplete or unverified`; do not invoke account-verified `rank` from that list. This warning does not auto-switch or interrupt the user-selected current configuration for a short task. When `available_complete=false` and strict mode is off, a validated Radar result may use `action=radar_only` to produce a public Radar-only recommendation from the known Codex model family: never emit a third-party Radar model. If the user explicitly enables `luna_quality_baseline` and public Radar contains Luna max, use that IQ as the explicit public Luna quality baseline; this does not establish Luna account availability. State that account availability is unverified, do not call it an account configuration, and continue the task. Strict mode keeps the complete-picker pause protection.
4. Before each `prepare`, re-derive `latency_priority` and `allow_fast` from the current task or active persistent preference. Never derive them from model history or Radar cache. All preference booleans default to `false`. Set `luna_max_fast_preference=true` only if the user explicitly enables it for this assessment or establishes an explicitly persistent Luna-only `max · Fast` preference; otherwise pass `false`. Set `luna_quality_baseline=true` only if the user explicitly enables it for this assessment or establishes an explicitly persistent quality-floor preference; otherwise pass `false`. Set `comparison_speed=standard`: all candidates are compared at Standard speed even if Luna max will be output as Fast. This preference does not automatically switch the current configuration and does not make a quality-qualified current Luna non-max configuration fail the quality gate. Derive `task_horizon` before `prepare`; default to `short` when duration is uncertain. Use `long` only when the user marks the task long, describes a batch or extended workflow, or the plan estimates at least 60 minutes.
5. Resolve the sibling `scripts/recommend.py` relative to the directory containing this `SKILL.md`, then run it with JSON stdin and `action=prepare`. Pass the app's compact model labels unchanged: known public Radar IDs such as `gpt-5.6-sol` and `gpt-5.6-terra` are normalized by the script before compatibility matching.
6. Continue to candidate scoring only when `prepare` returns `status=prepared`. Score every returned candidate on effort 0–8, workload 0–6, latency 0–4, and execution horizon 0–2; provide one short reason and do not score candidates outside the returned pool. Run the same script with `action=rank`, passing the complete `prepared` object and candidate fit map.
7. When `prepare` returns `warn` solely because the available model list is incomplete or unverified, strict mode is off, Radar data is valid, and qualified public candidates remain, run the same script with `action=radar_only` and the complete `prepared` object. Render `Radar-only 推荐：{model · effort · Standard/Fast}` plus one concise explanation that account availability is unverified. This is a public Radar-only recommendation, not a switch request; continue the task.
8. For other `warn`, `pause`, or `data_insufficient` results, obey the returned status and do not fabricate task-fit scores. If it reports `Luna max quality baseline is unavailable`, do not fall back to the ordinary risk floor or produce a new configuration recommendation. A pause ends the assessed task before implementation or external action.

## Modes

- Default: advise. `pause_on_change` retains pause protection for an unknown or unqualified current configuration; it does not pause a qualified task merely because a different qualified candidate scores higher or costs less.
- A valid default-mode Radar-only result is non-blocking even when `pause_on_change=true`; it is an explicitly labelled public comparison, not an account-verified switch decision.
- Outside strict mode, a qualified task does not pause solely because the recommended candidate or speed differs from the current configuration.
- Short tasks continue without a notice when the current configuration is qualified.
- The persistent Luna candidate rule alone does not pause a quality-qualified short task.
- For a long task, emit one non-blocking `模型差距提醒（不中断）` only when a qualified recommendation lowers Radar average price by at least 50%.
- The notice may be one concise normal-prose line headed `模型差距提醒（不中断）`; it reports the verified average-price comparison.
- This notice never pauses a qualified task by itself and never changes the active configuration.
- No notice is emitted for short tasks, an unqualified current configuration, savings below 50%, or when `notify_on_large_savings=false`.
- 关闭模型差距提醒 sets `notify_on_large_savings=false` for that task.
- Strict mode and an unknown or unqualified current configuration retain their existing pause protection. In strict mode, a materially better qualified candidate remains a pause case.
- Do not claim token savings from `average_price_usd`.
- `本次使用严格模式`: strict for this task only.
- `本任务持续严格`: strict for the current Codex task until disabled.
- `关闭严格模式`: return to default advice.

## Terminal state machine

Choose exactly one terminal state when ending the turn. Never combine a stopped preflight with a handoff. A preflight `continue` or `warn` may be reported in commentary while work proceeds. Every terminal state ends with exactly one footer of exactly two lines.

### `PREFLIGHT_STOP_KNOWN`

Use before execution when risk is knowable and prepare/rank or policy says to pause or refuse, or requires a configuration switch before work. Give the normal reason in prose, then use the unfinished-task footer. Its task line must name the concrete unblocking action, not repeat the blocked current task. Its model line contains the verified configuration needed to unblock work, or `无可验证推荐` when no verified recommendation exists.

### `PREFLIGHT_STOP_AMBIGUOUS`

Use before execution in strict mode when the task description plausibly spans two or more risk levels. Output exactly this two-line unfinished-task footer and nothing else:

```text
下一次任务：补充具体系统、操作、影响范围与回滚方案
推荐模型：无可验证推荐
```

Do not run `prepare` or `rank`, and do not access Radar. Instead pass this JSON to the sibling script's local renderer, then output its `block` field verbatim as the entire response:

```json
{"action": "render", "terminal_block": "strict_ambiguity"}
```

Do not add prose, a score, or another footer. Never output mojibake. The renderer is local and does not assess or rank candidates.

### `HANDOFF_NEXT_DEFINED`

Use after the current work is complete only when the overall task remains unfinished and the user or plan explicitly defines a next task. After the normal task result, append the unfinished-task footer for the next task only. Never recycle the completed current task.

### `HANDOFF_NEXT_SUGGESTED`

Use after the current work is complete when the overall task is complete and no explicit next task remains. From the just-completed result, proactively suggest exactly one grounded, adjacent, executable next task. This is an optional recommendation, not an inference about user intent. First assess that suggested task, then append the exactly two-line footer. Prefix the task value with `建议：` and require user confirmation before any execution. Do not execute the suggested task. Never output `下一任务未定义`. Never recycle the completed current task. Do not provide multiple next tasks or a task list.

## Unfinished-task footer

Use this footer for every terminal state, including a grounded suggestion after the overall task is complete:

```text
下一次任务：{明确的下一任务或解阻动作}
推荐模型：{model · effort · Standard/Fast，或 无可验证推荐}
```

The footer has exactly two lines. Copy the literal Unicode labels from the code block and Never output mojibake. Replace both placeholders with concrete values and remove the braces. The task must be the explicit next task from the user or plan, the concrete unblocking action for a stopped preflight, or the single grounded adjacent task required by `HANDOFF_NEXT_SUGGESTED`. The model must come from a successful qualified `rank` result, or a valid default-mode `radar_only` result that normal prose explicitly labels as account availability unverified; include model, effort, and Standard/Fast. Risk, quality gate, current configuration, action, confidence, and basis may appear in normal prose when useful, but never as extra footer lines.

### Force-L4 output

`force_l4=true` sets a minimum of 75, not a fixed score. Calculate the actual six-dimension score as `max(实际六维分数, 75)`. Report that risk and the L4 quality gate in normal prose when useful; the unfinished-task footer remains exactly two lines. This uses the actual six-dimension score, not a fixed 75.

```text
风险：L4，{max(实际六维分数, 75)}/100
下一次任务：{不可逆生产删除任务的解阻动作}
推荐模型：{经验证的 model · effort · Standard/Fast，或 无可验证推荐}
```

This is documentation notation: replace every placeholder, including the score expression, with the concrete value before output. The risk line is normal prose and is not part of the two-line footer.

### Data insufficiency

For `data_insufficient` or low Radar-data confidence, use `推荐模型：无可验证推荐` in the unfinished-task footer. The task line must still name a concrete explicit or suggested next task, or an unblocking action. Explain the data limitation in normal prose when useful. Do not copy the current configuration as the recommendation. Do not recommend a new configuration or report high confidence. A valid `radar_only` result is different: it may name a public comparison candidate only while clearly stating that account availability is unverified.

Keep terminal output concise. Do not expose hidden reasoning; report only scores, gates, data freshness, and decision factors.
