import http.client
import json
import math
import os
import re
import sys
import tempfile
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

RISK_WEIGHTS = {
    "reasoning": 25,
    "impact": 20,
    "reversibility": 15,
    "scope": 15,
    "uncertainty": 15,
    "verification": 10,
}

TERMINAL_BLOCKS = {
    "strict_ambiguity": (
        "下一次任务：补充具体系统、操作、影响范围与回滚方案\n"
        "推荐模型：无可验证推荐"
    ),
}


def render_terminal_block(name):
    if not isinstance(name, str) or name not in TERMINAL_BLOCKS:
        raise ValueError("unknown terminal block")
    return {"status": "rendered", "block": TERMINAL_BLOCKS[name]}


def level_for_score(score):
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        raise ValueError("risk score must be a finite number")
    if score < 0 or score > 100:
        raise ValueError("risk score must be from 0 to 100")
    if score < 25:
        return "L1"
    if score < 50:
        return "L2"
    if score < 75:
        return "L3"
    return "L4"


def calculate_risk(dimensions, force_l4=False):
    if not isinstance(dimensions, Mapping):
        raise ValueError("risk dimensions must be a mapping")
    if not isinstance(force_l4, bool):
        raise ValueError("force_l4 must be a boolean")
    if set(dimensions) != set(RISK_WEIGHTS):
        raise ValueError("risk dimensions must match the six required keys")
    for name, value in dimensions.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 4:
            raise ValueError(f"{name} must be an integer from 0 to 4")
    score = sum(dimensions[name] / 4.0 * weight for name, weight in RISK_WEIGHTS.items())
    if force_l4:
        score = max(score, 75.0)
    elif dimensions["uncertainty"] >= 3:
        for boundary in (25, 50, 75):
            if 0 < boundary - score <= 5:
                score = float(boundary)
                break
    score = round(score, 2)
    return {"score": score, "level": level_for_score(score)}


MAX_DATA_AGE_SECONDS = 24 * 60 * 60
RADAR_MODEL_ALIASES = {
    "gpt-5.6-sol": "sol",
    "gpt-5.6-terra": "terra",
    "gpt-5.6-luna": "luna",
}
KNOWN_CODEX_RADAR_MODELS = frozenset(RADAR_MODEL_ALIASES.values())
TASK_HORIZONS = {"short", "long"}
LARGE_SAVINGS_RATIO = 0.50


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid source_updated_at") from exc
    if parsed.tzinfo is None:
        raise ValueError("source_updated_at must include timezone")
    return parsed.astimezone(timezone.utc)


def _normalize_now(value):
    if value is None:
        return datetime.now(timezone.utc)
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def parse_policy(insights):
    if not isinstance(insights, Mapping):
        raise ValueError("insights must be a mapping")
    if type(insights.get("schema")) is not int or insights.get("schema") != 1:
        raise ValueError("unsupported radar-insights schema")
    recommendations = insights.get("recommendations")
    if not isinstance(recommendations, list):
        raise ValueError("invalid radar policy recommendations")
    rules = {}
    for item in recommendations:
        if (not isinstance(item, Mapping) or not isinstance(item.get("key"), str)
                or not isinstance(item.get("rule"), str)):
            raise ValueError("invalid radar policy recommendations")
        key = item["key"]
        if key in rules:
            raise ValueError("duplicate radar policy key")
        rules[key] = item["rule"]
    thresholds = {}
    patterns = (("L1", "lobster_tasks", r"IQ\s*≥\s*(\d+(?:\.\d+)?)，不设上限"),
                ("L2", "background_automation", r"成本优先：整数\s+IQ\s*≥\s*(\d+(?:\.\d+)?)"),
                ("L3", "daily_development", r"原始\s+IQ\s*≥\s*(\d+(?:\.\d+)?)"))
    for level, key, pattern in patterns:
        rule = rules.get(key)
        match = re.search(pattern, rule) if isinstance(rule, str) else None
        if not match:
            raise ValueError(f"unrecognized {key} quality rule")
        thresholds[level] = float(match.group(1))
    hard_problems = rules.get("hard_problems")
    top_match = (re.search(r"按\s+IQ\s+从高到低取\s*([1-9]\d*)\s*个", hard_problems)
                 if isinstance(hard_problems, str) else None)
    if not top_match:
        raise ValueError("unrecognized hard_problems quality rule")
    thresholds["L4_min"] = max(90.0, thresholds["L3"])
    thresholds["L4_top_n"] = int(top_match.group(1))
    return thresholds


def _validate_contract(metrics_data, insights_data, now):
    if not isinstance(metrics_data, Mapping) or not isinstance(insights_data, Mapping):
        raise ValueError("Radar payloads must be objects")
    if (type(metrics_data.get("schema")) is not int or metrics_data.get("schema") != 2
            or not isinstance(metrics_data.get("points"), list)):
        raise ValueError("unsupported metrics schema")
    for value in (metrics_data.get("source_updated_at"), insights_data.get("generated_at")):
        age = (now - parse_time(value)).total_seconds()
        if age < 0 or age > MAX_DATA_AGE_SECONDS:
            raise ValueError("stale Radar payload")
    return parse_policy(insights_data)


def _canonical_model(model):
    return RADAR_MODEL_ALIASES.get(model, model)


def _is_known_codex_radar_model(model):
    return _canonical_model(model) in KNOWN_CODEX_RADAR_MODELS


def candidate_key(model, effort, speed="standard"):
    if not all(isinstance(value, str) and value.strip() and "|" not in value
               for value in (model, effort, speed)):
        raise ValueError("candidate key parts must be non-empty strings without pipes")
    return f"{_canonical_model(model)}|{effort}|{speed}"


def _normalize_current(current):
    if current is None:
        return None
    if not isinstance(current, Mapping):
        raise ValueError("current must be a mapping or None")
    try:
        candidate_key(current.get("model"), current.get("effort"), current.get("speed"))
    except ValueError as exc:
        raise ValueError("current model, effort, and speed must be valid") from exc
    return {"model": _canonical_model(current["model"]),
            "effort": current["effort"], "speed": current["speed"]}


RUNTIME_CURRENT_SOURCES = {
    "codex_task_metadata",
    "user_supplied",
    "unspecified",
}


def _normalize_runtime_current(runtime_current):
    if runtime_current is None:
        return None
    if not isinstance(runtime_current, Mapping):
        raise ValueError("runtime_current must be a mapping or None")
    try:
        candidate_key(runtime_current.get("model"), runtime_current.get("effort"))
    except ValueError as exc:
        raise ValueError("runtime_current model and effort must be valid") from exc
    return {
        "model": _canonical_model(runtime_current["model"]),
        "effort": runtime_current["effort"],
    }


def _normalize_runtime_current_source(source, runtime_current):
    if runtime_current is None:
        if source is not None:
            raise ValueError("runtime_current_source requires runtime_current")
        return None
    if source is None:
        return "unspecified"
    if not isinstance(source, str) or source not in RUNTIME_CURRENT_SOURCES:
        raise ValueError(
            "runtime_current_source must be codex_task_metadata, user_supplied, or unspecified"
        )
    return source


def _payload_controls(payload):
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    controls = []
    for name in ("strict", "pause_on_change", "allow_fast",
                 "luna_max_fast_preference", "luna_quality_baseline"):
        value = payload.get(name, False)
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        controls.append(value)
    latency_priority = payload.get("latency_priority", "normal")
    if latency_priority not in ("normal", "high"):
        raise ValueError("latency_priority must be normal or high")
    comparison_speed = payload.get("comparison_speed", "standard")
    if comparison_speed != "standard":
        raise ValueError("comparison_speed must be standard")
    task_horizon = payload.get("task_horizon", "short")
    if not isinstance(task_horizon, str) or task_horizon not in TASK_HORIZONS:
        raise ValueError("task_horizon must be short or long")
    notify_on_large_savings = payload.get("notify_on_large_savings", True)
    if not isinstance(notify_on_large_savings, bool):
        raise ValueError("notify_on_large_savings must be a boolean")
    available_complete = payload.get("available_complete", False)
    if not isinstance(available_complete, bool):
        raise ValueError("available_complete must be a boolean")
    if available_complete:
        available_valid, _available_pairs = validated_pairs(payload.get("available"))
        if not available_valid:
            raise ValueError("available must be a valid list when available_complete is true")
    runtime_current = _normalize_runtime_current(payload.get("runtime_current"))
    runtime_current_source = _normalize_runtime_current_source(
        payload.get("runtime_current_source"), runtime_current
    )
    return (*controls, latency_priority, comparison_speed, task_horizon, notify_on_large_savings,
            available_complete, _normalize_current(payload.get("current")), runtime_current,
            runtime_current_source)


def validated_pairs(items):
    if not isinstance(items, list):
        return False, set()
    pairs = set()
    for item in items:
        if not isinstance(item, Mapping):
            return False, set()
        model = item.get("model")
        effort = item.get("effort")
        if not all(isinstance(value, str) and value.strip() and "|" not in value
                   for value in (model, effort)):
            return False, set()
        pairs.add((_canonical_model(model), effort))
    return True, pairs


def choose_speed(latency_priority, allow_fast):
    if latency_priority not in ("normal", "high"):
        raise ValueError("latency_priority must be normal or high")
    if not isinstance(allow_fast, bool):
        raise ValueError("allow_fast must be a boolean")
    return "fast" if latency_priority == "high" and allow_fast else "standard"


def _luna_effort_is_excluded(model, effort, luna_max_fast_preference):
    return (luna_max_fast_preference and _canonical_model(model) == "luna"
            and effort != "max")


def _candidate_speed(model, effort, latency_priority, allow_fast,
                     luna_max_fast_preference):
    if (luna_max_fast_preference and _canonical_model(model) == "luna"
            and effort == "max"):
        return "fast"
    return choose_speed(latency_priority, allow_fast)


def _candidate_speed_source(model, effort, latency_priority, allow_fast,
                            luna_max_fast_preference):
    if (luna_max_fast_preference and _canonical_model(model) == "luna"
            and effort == "max"):
        return "persistent_luna_max_fast_preference"
    return ("explicit_latency_preference"
            if choose_speed(latency_priority, allow_fast) == "fast" else "default")


def _luna_max_baseline_iq(candidates):
    values = [float(item["iq"]) for item in candidates
              if _canonical_model(item["model"]) == "luna" and item["effort"] == "max"]
    return max(values, default=None)


def prepare(payload, metrics, insights, now=None):
    if not isinstance(metrics, Mapping):
        raise ValueError("metrics must be a mapping")
    if not isinstance(insights, Mapping):
        raise ValueError("insights must be a mapping")
    (strict, pause_on_change, allow_fast, luna_max_fast_preference,
      luna_quality_baseline, latency_priority, comparison_speed, task_horizon,
      notify_on_large_savings, available_complete, current, runtime_current,
      runtime_current_source) = _payload_controls(payload)
    output_speed = choose_speed(latency_priority, allow_fast)
    if "risk_score" not in payload:
        raise ValueError("payload missing risk_score")
    now = _normalize_now(now)
    policy = _validate_contract(metrics, insights, now)
    risk_score = payload["risk_score"]
    risk_level = level_for_score(risk_score)
    risk = {"score": float(risk_score), "level": risk_level}
    compatibility_verified = available_complete
    _available_valid, allowed = validated_pairs(payload.get("available"))
    numeric_points = []
    for item in metrics["points"]:
        if not isinstance(item, dict):
            continue
        if not all(isinstance(value, str) and value.strip() and "|" not in value
                   for value in (item.get("model"), item.get("effort"))):
            continue
        values = [item.get("iq"), item.get("average_price_usd"),
                  item.get("average_minutes"), item.get("weighted_total")]
        if all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and value >= 0 for value in values):
            normalized = dict(item)
            normalized["model"] = _canonical_model(item["model"])
            numeric_points.append(normalized)
    compatible = (
        [item for item in numeric_points
         if (item.get("model"), item.get("effort")) in allowed]
        if available_complete else
        [item for item in numeric_points if _is_known_codex_radar_model(item["model"])]
    )
    fresh_points = []
    for item in compatible:
        try:
            age = (now - parse_time(item.get("source_updated_at"))).total_seconds()
        except ValueError:
            continue
        if 0 <= age <= MAX_DATA_AGE_SECONDS:
            fresh_points.append((item, age))
    deduplicated = {}
    for item, age in fresh_points:
        key = candidate_key(item["model"], item["effort"])
        quality = (-float(item["iq"]), float(item["average_price_usd"]),
                   float(item["average_minutes"]), -float(item["weighted_total"]), age)
        previous = deduplicated.get(key)
        if previous is None or quality < previous[0]:
            deduplicated[key] = (quality, item, age)
    fresh_points = [(item, age) for _quality, item, age in deduplicated.values()]
    max_samples = max((float(item["weighted_total"]) for item, _age in fresh_points), default=0)
    min_samples = max(100.0, max_samples * 0.25)
    degradation_alerts = insights.get("degradation_alerts")
    degradation_verified, degraded = validated_pairs(
        degradation_alerts.get("items") if isinstance(degradation_alerts, Mapping) else None)
    candidates = []
    for item, age in fresh_points:
        if item["weighted_total"] < min_samples:
            continue
        is_degraded = (item["model"], item["effort"]) in degraded
        if strict and is_degraded:
            continue
        candidates.append({
            "key": candidate_key(item["model"], item["effort"], output_speed),
            "model": item["model"],
            "effort": item["effort"],
            "speed": output_speed,
            "iq": float(item["iq"]),
            "price": float(item["average_price_usd"]),
            "minutes": float(item["average_minutes"]),
            "samples": float(item["weighted_total"]),
            "age_seconds": age,
            "degraded": is_degraded,
        })
    base_threshold = policy.get(risk["level"], policy["L4_min"])
    luna_baseline_iq = (
        _luna_max_baseline_iq(candidates) if luna_quality_baseline else None
    )
    baseline_unavailable = luna_quality_baseline and luna_baseline_iq is None
    threshold = None if baseline_unavailable else max(
        base_threshold,
        luna_baseline_iq if luna_quality_baseline else base_threshold)
    candidates = ([] if baseline_unavailable else [
        item for item in candidates if item["iq"] >= threshold])
    preference_excluded_quality_pairs = [
        {"model": item["model"], "effort": item["effort"]}
        for item in candidates
        if _luna_effort_is_excluded(
            item["model"], item["effort"], luna_max_fast_preference)
    ]
    candidates = [
        item for item in candidates
        if not _luna_effort_is_excluded(
            item["model"], item["effort"], luna_max_fast_preference)
    ]
    preference_qualified_current = (
        current is not None and (current["model"], current["effort"])
        in {(item["model"], item["effort"])
            for item in preference_excluded_quality_pairs}
    )
    for item in candidates:
        speed = _candidate_speed(
            item["model"], item["effort"], latency_priority, allow_fast,
            luna_max_fast_preference)
        item["speed"] = speed
        item["key"] = candidate_key(item["model"], item["effort"], speed)
    candidates.sort(key=lambda item: (-item["iq"], item["price"], item["minutes"],
                                      -item["samples"], item["age_seconds"], item["key"]))
    if risk["level"] == "L4":
        candidates = candidates[:policy["L4_top_n"]]
    reasons = []
    if not compatibility_verified:
        reasons.append("available model list is incomplete or unverified")
    if not degradation_verified:
        reasons.append("degradation alerts are unverified")
    if baseline_unavailable:
        reasons.append("Luna max quality baseline is unavailable")
    status = "prepared" if candidates or preference_qualified_current else "no_candidates"
    if baseline_unavailable:
        status = "pause" if strict else "warn"
    elif not available_complete:
        status = "pause" if strict else "warn"
    elif strict:
        if not candidates:
            reasons.append("no qualified candidates")
        if reasons:
            status = "pause"
    candidate_speed_sources = {
        _candidate_speed_source(
            item["model"], item["effort"], latency_priority, allow_fast,
            luna_max_fast_preference)
        for item in candidates
    }
    speed_source = (
        next(iter(candidate_speed_sources)) if len(candidate_speed_sources) == 1
        else "candidate_specific" if candidate_speed_sources
        else ("explicit_latency_preference" if output_speed == "fast" else "default")
    )
    return {
        "status": status,
        "risk": risk,
        "base_quality_floor": base_threshold,
        "quality_floor": threshold,
        "candidates": candidates,
        "current": current,
        "runtime_current": runtime_current,
        "runtime_current_source": runtime_current_source,
        "strict": strict,
        "pause_on_change": pause_on_change,
        "task_horizon": task_horizon,
        "notify_on_large_savings": notify_on_large_savings,
        "available_complete": available_complete,
        "compatibility_verified": compatibility_verified,
        "degradation_verified": degradation_verified,
        "allow_fast": allow_fast,
        "luna_max_fast_preference": luna_max_fast_preference,
        "luna_quality_baseline": luna_quality_baseline,
        "luna_quality_baseline_iq": luna_baseline_iq,
        "latency_priority": latency_priority,
        "comparison_speed": comparison_speed,
        "speed_source": speed_source,
        "preference_excluded_quality_pairs": preference_excluded_quality_pairs,
        "reasons": reasons,
    }


FIT_LIMITS = {"effort": 8, "workload": 6, "latency": 4, "execution_horizon": 2}


def _prepared_bool(prepared, name, default=False):
    if name not in prepared:
        return default
    value = prepared[name]
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _prepared_task_horizon(prepared):
    task_horizon = prepared.get("task_horizon", "short")
    if not isinstance(task_horizon, str) or task_horizon not in TASK_HORIZONS:
        raise ValueError("task_horizon must be short or long")
    return task_horizon


def _prepared_comparison_speed(prepared):
    comparison_speed = prepared.get("comparison_speed", "standard")
    if comparison_speed != "standard":
        raise ValueError("comparison_speed must be standard")
    return comparison_speed


def _prepared_quality_floor(prepared):
    value = prepared.get("quality_floor")
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0):
        raise ValueError("quality_floor must be a finite number at least 0")
    return float(value)


def _prepared_luna_quality_baseline(prepared, quality_floor):
    enabled = _prepared_bool(prepared, "luna_quality_baseline", False)
    if not enabled:
        return None
    value = prepared.get("luna_quality_baseline_iq")
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0):
        raise ValueError("luna_quality_baseline_iq must be a finite number at least 0")
    if quality_floor < value:
        raise ValueError("quality_floor must meet Luna max baseline")
    return float(value)


def _expected_speed(prepared, model=None, effort=None):
    return _candidate_speed(
        model, effort, prepared.get("latency_priority", "normal"),
        _prepared_bool(prepared, "allow_fast", False),
        _prepared_bool(prepared, "luna_max_fast_preference", False))


def _speed_source(prepared, candidate=None):
    if candidate is not None:
        return _candidate_speed_source(
            candidate["model"], candidate["effort"],
            prepared.get("latency_priority", "normal"),
            _prepared_bool(prepared, "allow_fast", False),
            _prepared_bool(prepared, "luna_max_fast_preference", False))
    return ("explicit_latency_preference" if _expected_speed(prepared) == "fast"
            else "default")


def _validated_candidates(prepared, require_verified_compatibility=True):
    if not isinstance(prepared, Mapping):
        raise ValueError("prepared must be a mapping")
    for name, default in (("strict", False), ("pause_on_change", False),
                          ("notify_on_large_savings", True),
                          ("available_complete", False),
                          ("compatibility_verified", False),
                          ("degradation_verified", False), ("allow_fast", False),
                          ("luna_max_fast_preference", False),
                          ("luna_quality_baseline", False)):
        _prepared_bool(prepared, name, default)
    if (require_verified_compatibility
            and (not _prepared_bool(prepared, "available_complete", False)
                 or not _prepared_bool(prepared, "compatibility_verified", False))):
        raise ValueError("available model list is incomplete or unverified")
    _prepared_task_horizon(prepared)
    _prepared_comparison_speed(prepared)
    quality_floor = _prepared_quality_floor(prepared)
    _prepared_luna_quality_baseline(prepared, quality_floor)
    latency_priority = prepared.get("latency_priority", "normal")
    allow_fast = _prepared_bool(prepared, "allow_fast", False)
    choose_speed(latency_priority, allow_fast)
    luna_max_fast_preference = _prepared_bool(
        prepared, "luna_max_fast_preference", False)
    raw_candidates = prepared.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise ValueError("candidates must be a list")
    candidates = []
    keys = set()
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, Mapping):
            raise ValueError("candidate must be a mapping")
        try:
            expected_key = candidate_key(raw_candidate.get("model"), raw_candidate.get("effort"),
                                         raw_candidate.get("speed"))
        except ValueError as exc:
            raise ValueError("candidate model, effort, and speed must be valid") from exc
        if raw_candidate.get("key") != expected_key:
            raise ValueError("candidate key must match model, effort, and speed")
        if _luna_effort_is_excluded(
                raw_candidate.get("model"), raw_candidate.get("effort"),
                luna_max_fast_preference):
            raise ValueError("candidate effort is not authorized")
        expected_speed = _expected_speed(
            prepared, raw_candidate.get("model"), raw_candidate.get("effort"))
        if raw_candidate.get("speed") != expected_speed:
            raise ValueError("candidate speed is not authorized")
        if expected_key in keys:
            raise ValueError("candidate keys must be unique")
        keys.add(expected_key)
        for name in ("iq", "price", "minutes", "samples", "age_seconds"):
            value = raw_candidate.get(name)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value)):
                raise ValueError(f"candidate {name} must be a finite number")
            if (name == "samples" and value <= 0) or (name != "samples" and value < 0):
                bound = "greater than 0" if name == "samples" else "at least 0"
                raise ValueError(f"candidate {name} must be {bound}")
        if raw_candidate["iq"] < quality_floor:
            raise ValueError("candidate iq is below quality floor")
        candidates.append(dict(raw_candidate))
    return candidates


def percentile_scores(values, higher_better=True):
    if not isinstance(values, (list, tuple)):
        raise ValueError("percentile values must be a list or tuple")
    if not isinstance(higher_better, bool):
        raise ValueError("percentile higher_better must be a boolean")
    if any(not isinstance(value, (int, float)) or isinstance(value, bool)
           or not math.isfinite(value) for value in values):
        raise ValueError("percentile values must be finite numbers")
    if not values:
        return []
    if len(values) == 1:
        return [100.0]
    ordered = sorted(enumerate(values), key=lambda pair: pair[1], reverse=higher_better)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average_rank = (index + end - 1) / 2.0
        for original_index, _value in ordered[index:end]:
            ranks[original_index] = average_rank
        index = end
    return [round(100.0 * (len(values) - 1 - rank) / (len(values) - 1), 6)
            for rank in ranks]


def relative_price_scores(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("relative price values must be a list or tuple")
    if any(not isinstance(value, (int, float)) or isinstance(value, bool)
           or not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("relative price values must be finite and non-negative")
    if not values:
        return []
    minimum = min(values)
    if minimum == max(values):
        return [100.0] * len(values)
    if minimum == 0:
        return [100.0 if value == 0 else 0.0 for value in values]
    return [round(100.0 * minimum / value, 6) for value in values]


def _fit_points(candidate_keys, task_fit):
    if not isinstance(task_fit, Mapping):
        raise ValueError("task-fit must be a mapping")
    if set(task_fit) != set(candidate_keys):
        raise ValueError("task-fit keys must match candidate keys")
    totals = {}
    for key in candidate_keys:
        entry = task_fit[key]
        if not isinstance(entry, Mapping):
            raise ValueError("task-fit entry must be a mapping")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise ValueError("task-fit reason is required")
        total = 0.0
        for name, maximum in FIT_LIMITS.items():
            value = entry.get(name)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value < 0 or value > maximum):
                raise ValueError(f"{name} must be from 0 to {maximum}")
            total += float(value)
        totals[key] = total
    return totals


def _score_radar_candidates(candidates):
    candidates = [dict(item) for item in candidates]
    iq_scores = percentile_scores([item["iq"] for item in candidates], True)
    price_scores = relative_price_scores([item["price"] for item in candidates])
    time_scores = percentile_scores([item["minutes"] for item in candidates], False)
    max_samples = max((item["samples"] for item in candidates), default=1.0)
    evidence_values = [
        0.8 * 100.0 * item["samples"] / max_samples
        + 0.2 * 100.0 * max(0.0, 1.0 - item["age_seconds"] / MAX_DATA_AGE_SECONDS)
        for item in candidates
    ]
    evidence_scores = percentile_scores(evidence_values, True)
    for index, item in enumerate(candidates):
        radar = (0.10 * iq_scores[index] + 0.70 * price_scores[index]
                 + 0.15 * time_scores[index] + 0.05 * evidence_scores[index])
        item["radar_score"] = radar
    return candidates


def rank_candidates(prepared, task_fit):
    candidates = _validated_candidates(prepared)
    keys = [item["key"] for item in candidates]
    fit_points = _fit_points(keys, task_fit)
    candidates = _score_radar_candidates(candidates)
    for item in candidates:
        item["task_fit_points"] = fit_points[item["key"]]
        item["total_score"] = item["radar_score"] * 0.80 + fit_points[item["key"]]
        item["task_fit_reason"] = task_fit[item["key"]]["reason"].strip()
    candidates.sort(key=lambda item: (-item["total_score"], -item["iq"], item["price"], item["key"]))
    return {"ranked": candidates}


def confidence_for(prepared):
    if not isinstance(prepared, Mapping):
        return "low"
    if (prepared.get("compatibility_verified") is not True
            or prepared.get("degradation_verified") is not True):
        return "low"
    data = prepared.get("data")
    if not isinstance(data, Mapping):
        return "low"
    age_seconds = data.get("age_seconds")
    if (not isinstance(age_seconds, (int, float)) or isinstance(age_seconds, bool)
            or not math.isfinite(age_seconds) or age_seconds < 0
            or age_seconds > MAX_DATA_AGE_SECONDS):
        return "low"
    if data.get("source") == "live":
        return "high"
    if data.get("source") == "cache":
        return "medium"
    return "low"


def _radar_data_confidence_for(prepared):
    if not isinstance(prepared, Mapping):
        return "low"
    if prepared.get("degradation_verified") is not True:
        return "low"
    data = prepared.get("data")
    if not isinstance(data, Mapping):
        return "low"
    age_seconds = data.get("age_seconds")
    if (not isinstance(age_seconds, (int, float)) or isinstance(age_seconds, bool)
            or not math.isfinite(age_seconds) or age_seconds < 0
            or age_seconds > MAX_DATA_AGE_SECONDS):
        return "low"
    if data.get("source") == "live":
        return "high"
    if data.get("source") == "cache":
        return "medium"
    return "low"


def _prepared_runtime_current(prepared):
    runtime_current = _normalize_runtime_current(prepared.get("runtime_current"))
    runtime_current_source = _normalize_runtime_current_source(
        prepared.get("runtime_current_source"), runtime_current
    )
    return runtime_current, runtime_current_source


def _radar_only_result(prepared, status, recommendation, ranked, reasons,
                       radar_data_confidence):
    runtime_current, runtime_current_source = _prepared_runtime_current(prepared)
    current = _normalize_current(prepared.get("current"))
    speed_source = (
        _speed_source(prepared, recommendation)
        if recommendation is not None else _speed_source(prepared)
    )
    return {
        "status": status,
        "mode": "radar_only",
        "recommendation_scope": "public_radar_only",
        "account_availability_verified": False,
        "risk": prepared.get("risk"),
        "quality_floor": prepared.get("quality_floor"),
        "recommendation": recommendation,
        "ranked": ranked,
        "current": current,
        "runtime_current": runtime_current,
        "runtime_current_source": runtime_current_source,
        "confidence": radar_data_confidence,
        "availability_confidence": "unverified",
        "radar_data_confidence": radar_data_confidence,
        "data": prepared.get("data", {}),
        "speed_source": speed_source,
        "reasons": reasons,
        "change_notice": None,
    }


def radar_only(prepared):
    if not isinstance(prepared, Mapping):
        raise ValueError("prepared must be a mapping")
    if prepared.get("status") not in ("prepared", "warn", "pause", "no_candidates"):
        raise ValueError("prepared data must have a recognized status")
    strict = _prepared_bool(prepared, "strict", False)
    available_complete = _prepared_bool(prepared, "available_complete", False)
    compatibility_verified = _prepared_bool(prepared, "compatibility_verified", False)
    if available_complete or compatibility_verified:
        raise ValueError(
            "radar_only requires an incomplete or unverified available model list"
        )
    candidates = _validated_candidates(
        prepared, require_verified_compatibility=False
    )
    radar_data_confidence = _radar_data_confidence_for(prepared)
    reasons = ["account availability is unverified"]
    if strict:
        reasons.append("strict mode requires a complete verified available model list")
        return _radar_only_result(
            prepared, "pause", None, [], reasons, radar_data_confidence
        )
    if radar_data_confidence == "low":
        reasons.append("Radar data is insufficient")
        return _radar_only_result(
            prepared, "warn", None, [], reasons, radar_data_confidence
        )
    if not candidates:
        reasons.append("no qualified public Radar candidates")
        return _radar_only_result(
            prepared, "warn", None, [], reasons, radar_data_confidence
        )
    ranked = _score_radar_candidates(candidates)
    ranked.sort(
        key=lambda item: (-item["radar_score"], -item["iq"], item["price"],
                          item["minutes"], item["key"])
    )
    return _radar_only_result(
        prepared, "continue", ranked[0], ranked, reasons, radar_data_confidence
    )


def _validated_current(prepared):
    current = _normalize_current(prepared.get("current"))
    if current is None:
        return None, None
    current_key = candidate_key(current["model"], current["effort"], current["speed"])
    return current, current_key


def _preference_excluded_quality_pairs(prepared):
    raw_pairs = prepared.get("preference_excluded_quality_pairs", [])
    valid, pairs = validated_pairs(raw_pairs)
    if not valid:
        raise ValueError("preference_excluded_quality_pairs must be a valid list")
    luna_max_fast_preference = _prepared_bool(
        prepared, "luna_max_fast_preference", False)
    if not luna_max_fast_preference and pairs:
        raise ValueError(
            "preference_excluded_quality_pairs require luna_max_fast_preference")
    if any(model != "luna" or effort == "max" for model, effort in pairs):
        raise ValueError("preference_excluded_quality_pairs must contain Luna non-max pairs")
    return pairs


def _large_savings_notice(current_item, best, task_horizon, notify_on_large_savings):
    if (not notify_on_large_savings or task_horizon != "long" or current_item is None
            or best["key"] == current_item["key"] or current_item["price"] <= 0
            or best["price"] >= current_item["price"]):
        return None
    savings_ratio = (current_item["price"] - best["price"]) / current_item["price"]
    if savings_ratio < LARGE_SAVINGS_RATIO:
        return None
    return {
        "recommended_key": best["key"],
        "current_price": current_item["price"],
        "recommended_price": best["price"],
        "savings_ratio": round(savings_ratio, 6),
        "non_blocking": True,
    }


def decide(prepared, task_fit):
    ranked = rank_candidates(prepared, task_fit)["ranked"]
    speed_source = _speed_source(prepared)
    strict = _prepared_bool(prepared, "strict")
    pause_on_change = _prepared_bool(prepared, "pause_on_change")
    task_horizon = _prepared_task_horizon(prepared)
    notify_on_large_savings = _prepared_bool(
        prepared, "notify_on_large_savings", True)
    compatibility_verified = _prepared_bool(prepared, "compatibility_verified")
    degradation_verified = _prepared_bool(prepared, "degradation_verified")
    confidence = confidence_for(prepared)
    current, current_key = _validated_current(prepared)
    preference_excluded_pairs = _preference_excluded_quality_pairs(prepared)
    if not ranked:
        current_qualified = (
            current is not None
            and (current["model"], current["effort"])
            in preference_excluded_pairs
        )
        reasons = [] if current_qualified else ["no qualified candidates"]
        if confidence == "low":
            reasons.append("recommendation data is insufficient")
        status = ("pause" if strict or (pause_on_change and not current_qualified)
                  else "warn" if reasons else "continue")
        return {"status": status,
                "risk": prepared.get("risk"), "quality_floor": prepared.get("quality_floor"),
                "recommendation": None, "current_qualified": current_qualified,
                "confidence": confidence, "data": prepared.get("data", {}),
                "speed_source": speed_source,
                "ranked": [], "reasons": reasons, "change_notice": None}
    by_key = {item["key"]: item for item in ranked}
    best = ranked[0]
    current_item = by_key.get(current_key)
    qualified_pairs = {
        (item["model"], item["effort"])
        for item in ranked
    }
    qualified_pairs.update(preference_excluded_pairs)
    current_qualified = (current is not None
                         and (current["model"], current["effort"]) in qualified_pairs)
    change_notice = None
    if confidence != "low" and current_qualified:
        change_notice = _large_savings_notice(
            current_item, best, task_horizon, notify_on_large_savings)
    recommendation = best
    if confidence == "low":
        recommendation = None
    elif (current_item and change_notice is None
          and best["total_score"] - current_item["total_score"] < 5.0):
        recommendation = current_item
    if recommendation is not None:
        speed_source = _speed_source(prepared, recommendation)
    reasons = []
    if confidence == "low":
        reasons.append("recommendation data is insufficient")
    if not compatibility_verified:
        reasons.append("compatibility is unverified")
    if not degradation_verified:
        reasons.append("degradation alerts are unverified")
    if current_key is None:
        reasons.append("current configuration is unknown")
    elif not current_qualified:
        reasons.append("current configuration failed the quality gate")
    changed = (recommendation is not None
               and (current_key is None or recommendation["key"] != current_key))
    if strict and (reasons or changed or not degradation_verified):
        status = "pause"
    elif pause_on_change and not current_qualified:
        status = "pause"
    elif reasons or change_notice is not None:
        status = "warn"
    else:
        status = "continue"
    return {"status": status, "risk": prepared.get("risk"),
            "quality_floor": prepared.get("quality_floor"),
            "recommendation": recommendation, "current_qualified": current_qualified,
            "confidence": confidence, "data": prepared.get("data", {}),
            "speed_source": speed_source,
            "ranked": ranked, "reasons": reasons,
            "change_notice": change_notice}


METRICS_URL = "https://codexradar.com/api/intelligence-efficiency-metrics"
INSIGHTS_URL = "https://codexradar.com/api/radar-insights"
CACHE_PATH = Path(tempfile.gettempdir()) / "choose-codex-model-cache.json"


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "choose-codex-model/1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response, parse_constant=_reject_json_constant)


def _reject_json_constant(_value):
    raise ValueError("invalid JSON constant")


def _normalize_cache_path(cache_path):
    try:
        return Path(cache_path)
    except (TypeError, ValueError) as exc:
        raise ValueError("cache_path must be a path") from exc


def _load_json(text):
    return json.loads(text, parse_constant=_reject_json_constant)


def _write_cache(cache_path, now, metrics_data, insights_data):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    body = {"saved_at": now.isoformat(), "metrics": metrics_data, "insights": insights_data}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(cache_path.parent),
                prefix=f".{cache_path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(body, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(cache_path))
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def load_radar_data(cache_path=CACHE_PATH, now=None, fetcher=fetch_json):
    cache_path = _normalize_cache_path(cache_path)
    now = _normalize_now(now)
    try:
        metrics_data = fetcher(METRICS_URL)
        insights_data = fetcher(INSIGHTS_URL)
        _validate_contract(metrics_data, insights_data, now)
        _write_cache(cache_path, now, metrics_data, insights_data)
        return {"source": "live", "metrics": metrics_data, "insights": insights_data,
                "age_seconds": 0.0}
    except (OSError, ValueError, KeyError, json.JSONDecodeError, http.client.HTTPException):
        try:
            cached = _load_json(cache_path.read_text(encoding="utf-8"))
            if not isinstance(cached, Mapping):
                raise ValueError("cache must be an object")
            age = (now - parse_time(cached["saved_at"])).total_seconds()
            _validate_contract(cached["metrics"], cached["insights"], now)
            if age < 0 or age > MAX_DATA_AGE_SECONDS:
                raise ValueError("cache expired")
            return {"source": "cache", "metrics": cached["metrics"],
                    "insights": cached["insights"], "age_seconds": age}
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("no valid live or cached Radar data") from exc


def run(payload, cache_path=CACHE_PATH, now=None, fetcher=fetch_json):
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    action = payload.get("action")
    if action == "rank":
        prepared = payload.get("prepared")
        if not isinstance(prepared, Mapping) or prepared.get("status") != "prepared":
            return {
                "status": "pause",
                "error": "prepared_not_rankable",
                "reasons": ["prepared data must have status 'prepared'"],
            }
        return decide(prepared, payload["task_fit_by_candidate"])
    if action == "radar_only":
        prepared = payload.get("prepared")
        if not isinstance(prepared, Mapping):
            return {
                "status": "pause",
                "error": "prepared_not_radar_only",
                "reasons": ["prepared data must be a mapping"],
            }
        return radar_only(prepared)
    if action == "render":
        return render_terminal_block(payload.get("terminal_block"))
    if action != "prepare":
        raise ValueError("action must be prepare, rank, radar_only, or render")
    (strict, pause_on_change, _allow_fast, _luna_max_fast_preference,
      _luna_quality_baseline, _latency_priority, comparison_speed, task_horizon,
      notify_on_large_savings, available_complete, current, runtime_current,
      runtime_current_source) = _payload_controls(payload)
    calculated_risk = calculate_risk(
        payload["risk_dimensions"], payload.get("force_l4", False))
    if "risk_score" in payload:
        risk_score = payload["risk_score"]
        risk = {"score": float(risk_score), "level": level_for_score(risk_score)}
    else:
        risk = calculated_risk
    prepared_payload = dict(payload)
    prepared_payload["risk_score"] = risk["score"]
    prepared_payload["current"] = current
    prepared_payload["runtime_current"] = runtime_current
    prepared_payload["runtime_current_source"] = runtime_current_source
    prepared_payload["task_horizon"] = task_horizon
    prepared_payload["notify_on_large_savings"] = notify_on_large_savings
    prepared_payload["available_complete"] = available_complete
    prepared_payload["comparison_speed"] = comparison_speed
    try:
        radar = load_radar_data(cache_path, now, fetcher)
    except ValueError as exc:
        return {
            "status": "pause" if strict or (pause_on_change and current is None) else "warn",
            "error": "data_insufficient",
            "risk": risk,
            "current": current,
            "runtime_current": runtime_current,
            "runtime_current_source": runtime_current_source,
            "confidence": "low",
            "data": {"source": "unavailable", "age_seconds": None},
            "reasons": [str(exc)],
        }
    result = prepare(prepared_payload, radar["metrics"], radar["insights"], now)
    result["data"] = {"source": radar["source"], "age_seconds": radar["age_seconds"]}
    result["confidence"] = confidence_for(result)
    if result["status"] == "no_candidates":
        result["status"] = "pause" if strict or pause_on_change else "warn"
    return result


def main():
    try:
        payload = json.load(sys.stdin, parse_constant=_reject_json_constant)
        result = run(payload)
        output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        code = 0
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        output = json.dumps({"status": "error", "error": str(exc)},
                            ensure_ascii=False, indent=2, allow_nan=False)
        code = 2
    sys.stdout.write(output + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
