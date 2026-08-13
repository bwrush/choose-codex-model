import ast
import importlib.util
import http.client
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).parents[1] / "scripts" / "recommend.py"
SPEC = importlib.util.spec_from_file_location("choose_codex_model_recommend", SCRIPT)
recommend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recommend)

NOW = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)


def point(
    model, effort, iq, price, minutes, samples=420, updated="2026-08-09T13:30:00+00:00"
):
    return {
        "model": model,
        "effort": effort,
        "iq": iq,
        "average_price_usd": price,
        "average_minutes": minutes,
        "weighted_total": samples,
        "source_updated_at": updated,
    }


def metrics(points):
    return {
        "schema": 2,
        "source_updated_at": "2026-08-09T13:30:00+00:00",
        "points": points,
    }


def insights(degraded=None, include_degradation=True):
    degraded = degraded or []

    result = {
        "schema": 1,
        "generated_at": "2026-08-09T13:30:00+00:00",
        "recommendations": [
            {"key": "lobster_tasks", "rule": "IQ ≥55，不设上限。"},
            {"key": "background_automation", "rule": "成本优先：整数 IQ ≥80。"},
            {"key": "daily_development", "rule": "原始 IQ ≥90。"},
            {"key": "hard_problems", "rule": "按 IQ 从高到低取 2 个。"},
        ],
    }
    if include_degradation:
        result["degradation_alerts"] = {"items": degraded}

    return result


def current_radar_insights():
    result = insights()

    rules = {
        "lobster_tasks": "IQ ≥55，不设上限，按费用与耗时综合成本最低取 2 个。",
        "background_automation": "成本优先：整数 IQ ≥80，不设上限，以平均费用最低者为基准；更高 IQ 至少领先 1.5 IQ，且每领先 1 IQ 可接受最多 2% 费用溢价；符合时优先 IQ 最高，依次取 2 个。",
        "daily_development": "原始 IQ ≥90，不再分档；在同一候选池中分别以平均耗时和费用与耗时综合成本为基准；更高 IQ 至少领先 1.5 IQ，且每领先 1 IQ 可接受最多 2% 对应指标溢价；去重后取 2 个。",
        "hard_problems": "所有有实测数据的档位中，按 IQ 从高到低取 2 个；IQ 相同时按模型顺序与综合成本排序。",
    }
    for item in result["recommendations"]:
        item["rule"] = rules[item["key"]]

    return result


class RiskTests(unittest.TestCase):
    def dimensions_for_score(self, reasoning):
        return {
            "reasoning": reasoning,
            "impact": 0,
            "reversibility": 0,
            "scope": 0,
            "uncertainty": 0,
            "verification": 0,
        }

    def test_risk_boundaries(self):
        cases = [(24, "L1"), (25, "L2"), (49, "L2"), (50, "L3"), (74, "L3"), (75, "L4")]

        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(recommend.level_for_score(score), expected)

    def test_uncertain_task_promotes_near_boundary(self):
        dimensions = {
            "reasoning": 3,
            "impact": 1,
            "reversibility": 0,
            "scope": 2,
            "uncertainty": 3,
            "verification": 1,
        }

        result = recommend.calculate_risk(dimensions)
        self.assertEqual(result["level"], "L3")

    def test_force_l4_sets_minimum_score(self):
        result = recommend.calculate_risk(self.dimensions_for_score(0), force_l4=True)
        self.assertEqual(result, {"score": 75.0, "level": "L4"})

    def test_invalid_dimension_rejected(self):
        dimensions = self.dimensions_for_score(5)

        with self.assertRaisesRegex(
            ValueError, "reasoning must be an integer from 0 to 4"
        ):
            recommend.calculate_risk(dimensions)

    def test_invalid_risk_scores_are_rejected(self):
        for score in (True, False, float("nan"), float("inf"), float("-inf"), "25"):
            with self.subTest(score=score):
                with self.assertRaisesRegex(
                    ValueError, "^risk score must be a finite number$"
                ):
                    recommend.level_for_score(score)

    def test_non_boolean_force_l4_is_rejected(self):
        for force_l4 in ("true", 1):
            with self.subTest(force_l4=force_l4):
                with self.assertRaisesRegex(ValueError, "^force_l4 must be a boolean$"):
                    recommend.calculate_risk(self.dimensions_for_score(0), force_l4)

    def test_non_mapping_dimensions_are_rejected(self):
        for dimensions in (None, []):
            with self.subTest(dimensions=dimensions):
                with self.assertRaisesRegex(
                    ValueError, "^risk dimensions must be a mapping$"
                ):
                    recommend.calculate_risk(dimensions)


class PrepareTests(unittest.TestCase):
    def payload(self, score, strict=False):
        return {
            "risk_score": score,
            "risk_dimensions": {
                "reasoning": 2,
                "impact": 2,
                "reversibility": 0,
                "scope": 2,
                "uncertainty": 0,
                "verification": 2,
            },
            "force_l4": False,
            "current": {"model": "sol", "effort": "high", "speed": "standard"},
            "available": [
                {"model": "sol", "effort": "high"},
                {"model": "sol", "effort": "xhigh"},
                {"model": "terra", "effort": "max"},
            ],
            "available_complete": True,
            "strict": strict,
            "pause_on_change": True,
            "task_horizon": "short",
            "notify_on_large_savings": True,
            "latency_priority": "normal",
            "allow_fast": False,
        }

    def test_policy_thresholds_are_parsed(self):
        self.assertEqual(
            recommend.parse_policy(insights()),
            {"L1": 55.0, "L2": 80.0, "L3": 90.0, "L4_min": 90.0, "L4_top_n": 2},
        )

    def test_current_radar_rules_keep_the_same_quality_semantics(self):
        self.assertEqual(
            recommend.parse_policy(current_radar_insights()),
            {"L1": 55.0, "L2": 80.0, "L3": 90.0, "L4_min": 90.0, "L4_top_n": 2},
        )

    def test_current_radar_model_ids_match_user_facing_available_models(self):
        data = metrics(
            [
                point("gpt-5.6-sol", "xhigh", 104, 6, 26),
                point("gpt-5.6-terra", "max", 96, 8, 24),
                point("gpt-5.6-luna", "max", 92, 3, 30),
            ]
        )

        payload = self.payload(60)
        payload["available"].append({"model": "luna", "effort": "max"})
        payload["current"] = {
            "model": "gpt-5.6-terra",
            "effort": "max",
            "speed": "standard",
        }

        prepared = recommend.prepare(payload, data, current_radar_insights(), NOW)

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]],
            ["sol|xhigh|standard", "terra|max|standard", "luna|max|standard"],
        )

        self.assertEqual(
            [item["model"] for item in prepared["candidates"]], ["sol", "terra", "luna"]
        )

        self.assertEqual(
            prepared["current"],
            {"model": "terra", "effort": "max", "speed": "standard"},
        )

    def test_partial_available_list_cannot_hide_luna_or_enable_ranking(self):
        data = metrics(
            [
                point("gpt-5.6-sol", "xhigh", 102.14, 6.290296, 25.91),
                point("gpt-5.6-terra", "xhigh", 84.64, 1.803446, 18.4),
                point("gpt-5.6-luna", "max", 93.57, 0.474308, 32.6),
            ]
        )

        responses = {
            recommend.METRICS_URL: data,
            recommend.INSIGHTS_URL: current_radar_insights(),
        }

        for marker in ("omitted", "false"):
            with self.subTest(marker=marker):
                payload = self.payload(42.5)
                payload["available"] = [
                    {"model": "gpt-5.6-sol", "effort": "xhigh"},
                    {"model": "gpt-5.6-terra", "effort": "xhigh"},
                ]
                if marker == "omitted":
                    payload.pop("available_complete")
                else:
                    payload["available_complete"] = False
                payload["current"] = {
                    "model": "gpt-5.6-terra",
                    "effort": "xhigh",
                    "speed": "standard",
                }
                prepared = recommend.prepare(
                    payload, data, current_radar_insights(), NOW
                )
                self.assertEqual(prepared["status"], "warn")
                self.assertFalse(prepared["available_complete"])
                self.assertFalse(prepared["compatibility_verified"])
                self.assertEqual(
                    prepared["reasons"],
                    ["available model list is incomplete or unverified"],
                )
                self.assertIn(
                    "luna|max|standard",
                    [item["key"] for item in prepared["candidates"]],
                )
                fit = {
                    item["key"]: {
                        "effort": 0,
                        "workload": 0,
                        "latency": 0,
                        "execution_horizon": 0,
                        "reason": "complete fit",
                    }
                    for item in prepared["candidates"]
                }
                with self.assertRaisesRegex(
                    ValueError, "^available model list is incomplete or unverified$"
                ):
                    recommend.rank_candidates(prepared, fit)
            request = dict(payload, action="prepare")
            with tempfile.TemporaryDirectory() as directory:
                public = recommend.run(
                    request,
                    cache_path=Path(directory) / "radar.json",
                    now=NOW,
                    fetcher=(lambda url: responses[url]),
                )

            self.assertEqual(public["status"], "warn")
            self.assertEqual(public["confidence"], "low")
            self.assertFalse(public["available_complete"])
            self.assertFalse(public["compatibility_verified"])

            self.assertEqual(
                public["reasons"], ["available model list is incomplete or unverified"]
            )

            self.assertEqual(public["risk"], {"score": 42.5, "level": "L2"})

            self.assertEqual(public["data"]["source"], "live")
            self.assertNotIn("recommendation", public)

            self.assertEqual(
                public["current"],
                {"model": "terra", "effort": "xhigh", "speed": "standard"},
            )

            self.assertIn(
                "luna|max|standard", [item["key"] for item in public["candidates"]]
            )

            ranked = recommend.run(
                {"action": "rank", "prepared": prepared, "task_fit_by_candidate": fit}
            )

            self.assertEqual(ranked["status"], "pause")
            self.assertEqual(ranked["error"], "prepared_not_rankable")

    def test_complete_available_list_can_exclude_luna(self):
        payload = self.payload(42.5)
        payload["available"] = [
            {"model": "gpt-5.6-sol", "effort": "xhigh"},
            {"model": "gpt-5.6-terra", "effort": "xhigh"},
        ]
        payload["current"] = {
            "model": "gpt-5.6-terra",
            "effort": "xhigh",
            "speed": "standard",
        }

        data = metrics(
            [
                point("gpt-5.6-sol", "xhigh", 102.14, 6.290296, 25.91),
                point("gpt-5.6-terra", "xhigh", 84.64, 1.803446, 18.4),
                point("gpt-5.6-luna", "max", 93.57, 0.474308, 32.6),
            ]
        )

        prepared = recommend.prepare(payload, data, current_radar_insights(), NOW)
        self.assertIn("available_complete", prepared)
        self.assertTrue(prepared["available_complete"])
        self.assertTrue(prepared["compatibility_verified"])
        self.assertEqual(prepared["status"], "prepared")

        self.assertNotIn(
            "luna|max|standard", [item["key"] for item in prepared["candidates"]]
        )

        fit = {
            item["key"]: {
                "effort": 0,
                "workload": 0,
                "latency": 0,
                "execution_horizon": 0,
                "reason": "complete fit",
            }
            for item in prepared["candidates"]
        }

        self.assertEqual(len(recommend.rank_candidates(prepared, fit)["ranked"]), 2)

    def test_prepare_rejects_non_boolean_available_complete(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        for invalid in (None, 0, 1, "true"):
            with self.subTest(invalid=invalid):
                payload = self.payload(60)
                payload["available_complete"] = invalid
                with self.assertRaisesRegex(
                    ValueError, "^available_complete must be a boolean$"
                ):
                    recommend.prepare(payload, data, insights(), NOW)

    def test_current_radar_rules_apply_l4_filter_after_quality_gate(self):
        prepared = recommend.prepare(
            self.payload(75),
            metrics(
                [
                    point("gpt-5.6-sol", "xhigh", 104, 6, 26),
                    point("gpt-5.6-terra", "max", 96, 8, 24),
                    point("gpt-5.6-sol", "high", 91, 4, 20),
                ]
            ),
            current_radar_insights(),
            NOW,
        )

        self.assertEqual(prepared["quality_floor"], 90.0)

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]],
            ["sol|xhigh|standard", "terra|max|standard"],
        )

    def test_l4_floor_never_drops_below_spec_minimum_when_l3_policy_changes(self):
        changed = insights()
        changed["recommendations"][2]["rule"] = "原始 IQ ≥88。"

        self.assertEqual(recommend.parse_policy(changed)["L4_min"], 90.0)

    def test_changed_policy_rule_is_rejected(self):
        changed = insights()
        changed["recommendations"][2]["rule"] = "use a strong model"

        with self.assertRaisesRegex(
            ValueError, "unrecognized daily_development quality rule"
        ):
            recommend.parse_policy(changed)

    def test_l1_l2_l3_quality_floors_are_applied(self):
        for score, floor in ((0, 55), (25, 80), (50, 90)):
            with self.subTest(score=score, floor=floor):
                data = metrics(
                    [
                        point("sol", "high", floor - 1, 4, 20),
                        point("sol", "xhigh", floor, 6, 26),
                    ]
                )
                prepared = recommend.prepare(self.payload(score), data, insights(), NOW)
                self.assertEqual(prepared["quality_floor"], floor)
                self.assertEqual(
                    [item["key"] for item in prepared["candidates"]],
                    ["sol|xhigh|standard"],
                )

    def test_unsupported_metrics_schema_is_rejected(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        for schema in (3, 2.0):
            with self.subTest(schema=schema):
                data["schema"] = schema
                with self.assertRaisesRegex(ValueError, "unsupported metrics schema"):
                    recommend.prepare(self.payload(60), data, insights(), NOW)

    def test_l3_filters_below_90_and_unavailable(self):
        data = metrics(
            [
                point("sol", "high", 93, 4, 20),
                point("sol", "xhigh", 109, 6, 26),
                point("terra", "max", 96, 3, 30),
                point("luna", "max", 94, 0.5, 32),
                point("sol", "medium", 89, 3, 17),
            ]
        )

        prepared = recommend.prepare(self.payload(60), data, insights(), NOW)

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]],
            ["sol|xhigh|standard", "terra|max|standard", "sol|high|standard"],
        )

    def test_l4_takes_top_two_after_compatibility(self):
        data = metrics(
            [
                point("unavailable", "max", 120, 1, 10),
                point("sol", "xhigh", 109, 6, 26),
                point("sol", "high", 101, 4, 20),
                point("terra", "max", 96, 3, 30),
            ]
        )

        prepared = recommend.prepare(self.payload(80), data, insights(), NOW)

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]],
            ["sol|xhigh|standard", "sol|high|standard"],
        )

    def test_strict_mode_excludes_degraded_candidate(self):
        data = metrics(
            [point("sol", "high", 93, 4, 20), point("sol", "xhigh", 109, 6, 26)]
        )

        alert = [{"model": "sol", "effort": "xhigh"}]
        prepared = recommend.prepare(
            self.payload(60, strict=True), data, insights(alert), NOW
        )

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]], ["sol|high|standard"]
        )

    def test_stale_and_small_samples_are_removed(self):
        data = metrics(
            [
                point("sol", "high", 93, 4, 20, samples=420),
                point("sol", "xhigh", 109, 6, 26, samples=99),
                point("terra", "max", 96, 3, 30, updated="2026-08-08T12:00:00+00:00"),
            ]
        )

        prepared = recommend.prepare(self.payload(60), data, insights(), NOW)

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]], ["sol|high|standard"]
        )

    def test_nonfinite_and_malformed_points_do_not_poison_sample_threshold(self):
        data = metrics(
            [
                point("sol", "high", 93, 4, 20, samples=420),
                point("sol", "xhigh", 109, 6, 26, samples=float("inf")),
                point("terra", "max", 96, 3, 30, updated="not-a-time"),
                point(None, "max", 96, 3, 30),
            ]
        )

        prepared = recommend.prepare(self.payload(60), data, insights(), NOW)

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]], ["sol|high|standard"]
        )

    def test_missing_degradation_alerts_has_mode_specific_behavior(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        prepared = recommend.prepare(
            self.payload(60), data, insights(include_degradation=False), NOW
        )

        self.assertEqual(prepared["status"], "prepared")
        self.assertFalse(prepared["degradation_verified"])

        strict = recommend.prepare(
            self.payload(60, strict=True),
            data,
            insights(include_degradation=False),
            NOW,
        )

        self.assertEqual(strict["status"], "pause")

    def test_strict_unknown_compatibility_pauses_before_fit_scoring(self):
        data = metrics([point("sol", "high", 93, 4, 20)])
        payload = self.payload(60, strict=True)
        payload["available"] = None
        payload["available_complete"] = False

        prepared = recommend.prepare(payload, data, insights(), NOW)
        self.assertEqual(prepared["status"], "pause")

        self.assertIn(
            "available model list is incomplete or unverified", prepared["reasons"]
        )

    def test_empty_candidate_pool_has_mode_specific_status(self):
        data = metrics([point("sol", "high", 89, 4, 20)])

        self.assertEqual(
            recommend.prepare(self.payload(60), data, insights(), NOW)["status"],
            "no_candidates",
        )

        strict = recommend.prepare(self.payload(60, strict=True), data, insights(), NOW)
        self.assertEqual(strict["status"], "pause")
        self.assertIn("no qualified candidates", strict["reasons"])

    def test_prepare_rejects_invalid_risk_scores_without_coercion(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        for score in (True, False, float("nan"), float("inf"), float("-inf"), "60"):
            with self.subTest(score=score):
                with self.assertRaisesRegex(
                    ValueError, "^risk score must be a finite number$"
                ):
                    recommend.prepare(self.payload(score), data, insights(), NOW)

    def test_prepare_validates_risk_before_float_conversion(self):
        class FloatableInvalidScore:
            def __float__(self):
                raise AssertionError("invalid score must not be converted")

        data = metrics([point("sol", "high", 93, 4, 20)])

        with self.assertRaisesRegex(ValueError, "^risk score must be a finite number$"):
            recommend.prepare(
                self.payload(FloatableInvalidScore()), data, insights(), NOW
            )

    def test_policy_rules_must_match_known_semantics(self):
        cases = (
            (2, "Ignore IQ ≥90; choose any model", "daily_development"),
            (3, "按价格由低至高取 2 个", "hard_problems"),
            (3, "按 IQ 从高到低取 0 个。", "hard_problems"),
        )

        for index, rule, key in cases:
            with self.subTest(rule=rule):
                changed = insights()
                changed["recommendations"][index]["rule"] = rule
                with self.assertRaisesRegex(
                    ValueError, f"unrecognized {key} quality rule"
                ):
                    recommend.parse_policy(changed)

    def test_incomplete_available_marker_warns_without_treating_the_list_as_an_allowlist(
        self,
    ):
        data = metrics([point("sol", "high", 93, 4, 20)])
        payload = self.payload(60)
        payload["available"] = [{"model": "sol", "effort": "high"}]
        payload["available_complete"] = False

        prepared = recommend.prepare(payload, data, insights(), NOW)
        self.assertFalse(prepared["compatibility_verified"])
        self.assertEqual(prepared["status"], "warn")

        self.assertIn(
            "available model list is incomplete or unverified", prepared["reasons"]
        )

        strict_payload = self.payload(60, strict=True)
        strict_payload["available"] = [{"model": "sol", "effort": "high"}]
        strict_payload["available_complete"] = False

        strict = recommend.prepare(strict_payload, data, insights(), NOW)
        self.assertEqual(strict["status"], "pause")

        self.assertIn(
            "available model list is incomplete or unverified", strict["reasons"]
        )

    def test_malformed_degradation_entries_disable_verification(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        malformed = (
            None,
            {"model": "", "effort": "high"},
            {"model": "sol", "effort": 1},
            {"model": "sol", "effort": "high|bad"},
        )

        for entry in malformed:
            with self.subTest(entry=entry):
                source = insights()
                source["degradation_alerts"]["items"] = [entry]
                prepared = recommend.prepare(self.payload(60), data, source, NOW)
                self.assertFalse(prepared["degradation_verified"])
                self.assertEqual(prepared["status"], "prepared")
                strict = recommend.prepare(
                    self.payload(60, strict=True), data, source, NOW
                )
                self.assertEqual(strict["status"], "pause")
                self.assertIn("degradation alerts are unverified", strict["reasons"])

    def test_candidate_key_rejects_malformed_parts(self):
        self.assertEqual(recommend.candidate_key("sol", "high"), "sol|high|standard")

        for parts in (
            ("", "high", "standard"),
            ("sol", None, "standard"),
            ("sol|bad", "high", "standard"),
            ("sol", "high", "fast|bad"),
        ):
            with self.subTest(parts=parts):
                with self.assertRaises(ValueError):
                    recommend.candidate_key(*parts)

    def test_malformed_metric_key_parts_are_skipped(self):
        data = metrics(
            [
                point("sol|bad", "high", 120, 1, 10),
                point("sol", "high|bad", 119, 1, 10),
                point("sol", "high", 93, 4, 20),
            ]
        )

        prepared = recommend.prepare(self.payload(60), data, insights(), NOW)

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]], ["sol|high|standard"]
        )

    def test_duplicate_metrics_choose_deterministically_regardless_of_input_order(self):
        cases = (
            (
                point("sol", "high", 94, 6, 26, updated="2026-08-09T13:00:00+00:00"),
                point(
                    "sol",
                    "high",
                    93,
                    1,
                    1,
                    samples=999,
                    updated="2026-08-09T13:59:00+00:00",
                ),
            ),
            (point("sol", "high", 93, 5, 26), point("sol", "high", 93, 6, 26)),
            (point("sol", "high", 93, 5, 25), point("sol", "high", 93, 5, 26)),
            (
                point("sol", "high", 93, 5, 25, samples=421),
                point("sol", "high", 93, 5, 25, samples=420),
            ),
            (
                point("sol", "high", 93, 5, 25, updated="2026-08-09T13:50:00+00:00"),
                point("sol", "high", 93, 5, 25, updated="2026-08-09T13:00:00+00:00"),
            ),
        )

        for preferred, other in cases:
            with self.subTest(preferred=preferred):
                forward = recommend.prepare(
                    self.payload(60), metrics([preferred, other]), insights(), NOW
                )
                reverse = recommend.prepare(
                    self.payload(60), metrics([other, preferred]), insights(), NOW
                )
                self.assertEqual(forward["candidates"], reverse["candidates"])
                candidate = forward["candidates"][0]
                self.assertEqual(
                    (
                        candidate["iq"],
                        candidate["price"],
                        candidate["minutes"],
                        candidate["samples"],
                    ),
                    (
                        float(preferred["iq"]),
                        float(preferred["average_price_usd"]),
                        float(preferred["average_minutes"]),
                        float(preferred["weighted_total"]),
                    ),
                )

    def test_candidate_order_uses_all_declared_tiebreakers(self):
        payload = self.payload(60)

        payload["available"].extend(
            [
                {"model": "luna", "effort": "max"},
                {"model": "alpha", "effort": "max"},
                {"model": "zeta", "effort": "max"},
            ]
        )

        data = metrics(
            [
                point("sol", "high", 93, 4, 20, samples=450),
                point("sol", "xhigh", 93, 4, 20, updated="2026-08-09T13:00:00+00:00"),
                point("terra", "max", 93, 4, 10),
                point("luna", "max", 93, 4, 20, updated="2026-08-09T13:50:00+00:00"),
                point("zeta", "max", 93, 4, 20),
                point("alpha", "max", 93, 4, 20),
            ]
        )

        prepared = recommend.prepare(payload, data, insights(), NOW)

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]],
            [
                "terra|max|standard",
                "sol|high|standard",
                "luna|max|standard",
                "alpha|max|standard",
                "zeta|max|standard",
                "sol|xhigh|standard",
            ],
        )

    def test_prepare_rejects_naive_or_non_datetime_now(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        for now in (datetime(2026, 8, 9, 14, 0), "now", 0):
            with self.subTest(now=now):
                with self.assertRaisesRegex(
                    ValueError, "^now must be a timezone-aware datetime$"
                ):
                    recommend.prepare(self.payload(60), data, insights(), now)

        for field, value in (
            ("source_updated_at", None),
            ("source_updated_at", "2026-08-08T10:00:00+00:00"),
            ("source_updated_at", "2026-08-09T14:01:00+00:00"),
            ("generated_at", None),
            ("generated_at", "2026-08-08T10:00:00+00:00"),
            ("generated_at", "2026-08-09T14:01:00+00:00"),
        ):
            with self.subTest(field=field, value=value):
                changed_data = metrics([point("sol", "high", 93, 4, 20)])
                source = insights()
                (changed_data if field == "source_updated_at" else source)[
                    field
                ] = value
                with self.assertRaisesRegex(
                    ValueError, "stale Radar payload|invalid source_updated_at"
                ):
                    recommend.prepare(self.payload(60), changed_data, source, NOW)

    def test_prepare_requires_mapping_inputs(self):
        valid_payload = self.payload(60)
        valid_metrics = metrics([point("sol", "high", 93, 4, 20)])
        valid_insights = insights()

        cases = (
            (None, valid_metrics, valid_insights, "payload must be a mapping"),
            (valid_payload, [], valid_insights, "metrics must be a mapping"),
            (valid_payload, valid_metrics, "radar", "insights must be a mapping"),
        )

        for payload, data, source, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, f"^{message}$"):
                    recommend.prepare(payload, data, source, NOW)

    def test_strict_must_be_boolean_and_defaults_to_false(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        for strict in (0, 1, "false"):
            with self.subTest(strict=strict):
                payload = self.payload(60)
                payload["strict"] = strict
                with self.assertRaisesRegex(ValueError, "^strict must be a boolean$"):
                    recommend.prepare(payload, data, insights(), NOW)
        payload = self.payload(60)
        del payload["strict"]

        self.assertFalse(recommend.prepare(payload, data, insights(), NOW)["strict"])

    def test_schemas_require_exact_integer_values(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        for schema in (True, 2.0):
            with self.subTest(metrics_schema=schema):
                changed = metrics([point("sol", "high", 93, 4, 20)])
                changed["schema"] = schema
                with self.assertRaisesRegex(ValueError, "^unsupported metrics schema$"):
                    recommend.prepare(self.payload(60), changed, insights(), NOW)

        for schema in (True, 1.0):
            with self.subTest(insights_schema=schema):
                source = insights()
                source["schema"] = schema
                with self.assertRaisesRegex(
                    ValueError, "^unsupported radar-insights schema$"
                ):
                    recommend.prepare(self.payload(60), data, source, NOW)

    def test_policy_recommendations_require_complete_unique_mappings(self):
        for recommendations in (None, [{}]):
            with self.subTest(recommendations=recommendations):
                source = insights()
                source["recommendations"] = recommendations
                with self.assertRaisesRegex(
                    ValueError, "^invalid radar policy recommendations$"
                ):
                    recommend.parse_policy(source)
        duplicate = insights()

        duplicate["recommendations"].append(
            {"key": "lobster_tasks", "rule": "IQ ≥55，不设上限。"}
        )

        with self.assertRaisesRegex(ValueError, "^duplicate radar policy key$"):
            recommend.parse_policy(duplicate)
        missing = insights()
        missing["recommendations"] = missing["recommendations"][:2]

        with self.assertRaisesRegex(
            ValueError, "unrecognized daily_development quality rule"
        ):
            recommend.parse_policy(missing)

    def test_optional_boolean_payload_fields_are_validated(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        for field, message in (
            ("pause_on_change", "pause_on_change must be a boolean"),
            ("allow_fast", "allow_fast must be a boolean"),
        ):
            with self.subTest(field=field, default=True):
                payload = self.payload(60)
                del payload[field]
                self.assertFalse(
                    recommend.prepare(payload, data, insights(), NOW)[field]
                )

            for value in (0, 1, "false"):
                with self.subTest(field=field, value=value):
                    payload = self.payload(60)
                    payload[field] = value

                    with self.assertRaisesRegex(ValueError, f"^{message}$"):
                        recommend.prepare(payload, data, insights(), NOW)

        for latency_priority in (float("nan"), "urgent"):
            with self.subTest(latency_priority=latency_priority):
                payload = self.payload(60)
                payload["latency_priority"] = latency_priority
                with self.assertRaisesRegex(
                    ValueError, "latency_priority must be normal or high"
                ):
                    recommend.prepare(payload, data, insights(), NOW)

    def test_adaptive_notice_payload_controls_have_defaults(self):
        data = metrics([point("sol", "high", 93, 4, 20)])
        payload = self.payload(60)
        del payload["task_horizon"]
        del payload["notify_on_large_savings"]

        prepared = recommend.prepare(payload, data, insights(), NOW)
        self.assertEqual(prepared.get("task_horizon"), "short")
        self.assertIs(prepared.get("notify_on_large_savings"), True)

    def test_adaptive_notice_payload_controls_default_independently(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        cases = (
            (
                "long horizon defaults notices on",
                "notify_on_large_savings",
                {"task_horizon": "long"},
                "long",
                True,
            ),
            (
                "disabled notices default horizon short",
                "task_horizon",
                {"notify_on_large_savings": False},
                "short",
                False,
            ),
        )

        for label, missing_field, explicit, expected_horizon, expected_notify in cases:
            with self.subTest(label=label):
                payload = self.payload(60)
                del payload[missing_field]
                payload.update(explicit)
                prepared = recommend.prepare(payload, data, insights(), NOW)
                self.assertEqual(prepared.get("task_horizon"), expected_horizon)
                self.assertIs(prepared.get("notify_on_large_savings"), expected_notify)

    def test_adaptive_notice_payload_controls_are_validated(self):
        data = metrics([point("sol", "high", 93, 4, 20)])

        for task_horizon in ("short", "long"):
            with self.subTest(task_horizon=task_horizon):
                payload = self.payload(60)
                payload["task_horizon"] = task_horizon
                self.assertEqual(
                    recommend.prepare(payload, data, insights(), NOW).get(
                        "task_horizon"
                    ),
                    task_horizon,
                )

        for task_horizon in (None, "medium", True):
            with self.subTest(task_horizon=task_horizon):
                payload = self.payload(60)
                payload["task_horizon"] = task_horizon
                with self.assertRaisesRegex(
                    ValueError, "^task_horizon must be short or long$"
                ):
                    recommend.prepare(payload, data, insights(), NOW)

        for notify_on_large_savings in (True, False):
            with self.subTest(notify_on_large_savings=notify_on_large_savings):
                payload = self.payload(60)
                payload["notify_on_large_savings"] = notify_on_large_savings
                self.assertIs(
                    recommend.prepare(payload, data, insights(), NOW).get(
                        "notify_on_large_savings"
                    ),
                    notify_on_large_savings,
                )

        for notify_on_large_savings in (None, 0, 1, "true"):
            with self.subTest(notify_on_large_savings=notify_on_large_savings):
                payload = self.payload(60)
                payload["notify_on_large_savings"] = notify_on_large_savings
                with self.assertRaisesRegex(
                    ValueError, "^notify_on_large_savings must be a boolean$"
                ):
                    recommend.prepare(payload, data, insights(), NOW)

    def test_payload_missing_risk_score_is_rejected(self):
        payload = self.payload(60)
        del payload["risk_score"]

        data = metrics([point("sol", "high", 93, 4, 20)])

        with self.assertRaisesRegex(ValueError, "^payload missing risk_score$"):
            recommend.prepare(payload, data, insights(), NOW)


class RankingTests(unittest.TestCase):
    def prepared(self):
        return {
            "status": "prepared",
            "risk": {"score": 60.0, "level": "L3"},
            "quality_floor": 90.0,
            "candidates": [
                {
                    "key": "sol|xhigh|standard",
                    "model": "sol",
                    "effort": "xhigh",
                    "speed": "standard",
                    "iq": 109.0,
                    "price": 6.0,
                    "minutes": 26.0,
                    "samples": 420.0,
                    "age_seconds": 600.0,
                    "degraded": False,
                },
                {
                    "key": "luna|max|standard",
                    "model": "luna",
                    "effort": "max",
                    "speed": "standard",
                    "iq": 94.0,
                    "price": 0.5,
                    "minutes": 32.0,
                    "samples": 420.0,
                    "age_seconds": 600.0,
                    "degraded": False,
                },
            ],
            "current": {"model": "sol", "effort": "xhigh", "speed": "standard"},
            "strict": False,
            "pause_on_change": True,
            "task_horizon": "short",
            "notify_on_large_savings": True,
            "available_complete": True,
            "compatibility_verified": True,
            "degradation_verified": True,
            "allow_fast": False,
            "latency_priority": "normal",
            "data": {"source": "live", "age_seconds": 0.0},
        }

    def fits(self, sol=20, luna=10):
        return {
            "sol|xhigh|standard": {
                "effort": min(sol, 8),
                "workload": min(max(sol - 8, 0), 6),
                "latency": min(max(sol - 14, 0), 4),
                "execution_horizon": min(max(sol - 18, 0), 2),
                "reason": "deep task",
            },
            "luna|max|standard": {
                "effort": min(luna, 8),
                "workload": min(max(luna - 8, 0), 6),
                "latency": min(max(luna - 14, 0), 4),
                "execution_horizon": min(max(luna - 18, 0), 2),
                "reason": "cost fit",
            },
        }

    def test_percentile_ties_use_average_rank(self):
        scores = recommend.percentile_scores([10, 10, 5], higher_better=True)
        self.assertEqual(scores, [75.0, 75.0, 0.0])
        self.assertEqual(recommend.percentile_scores([1, 2], True), [0.0, 100.0])
        self.assertEqual(recommend.percentile_scores([1, 2], False), [100.0, 0.0])
        self.assertEqual(recommend.percentile_scores([7], True), [100.0])

    def test_cost_first_radar_weights_and_total_score_prefer_luna(self):
        prepared = self.prepared()
        fits = self.fits(sol=0, luna=0)

        self.assertTrue(
            all(
                (
                    item["iq"] >= prepared["quality_floor"]
                    for item in prepared["candidates"]
                )
            )
        )

        result = recommend.rank_candidates(prepared, fits)
        ranked = {item["key"]: item for item in result["ranked"]}

        expected_radar = {"sol|xhigh|standard": 33.3333331, "luna|max|standard": 72.5}

        expected_fit = {"sol|xhigh|standard": 0.0, "luna|max|standard": 0.0}

        for key in expected_radar:
            with self.subTest(key=key):
                self.assertAlmostEqual(ranked[key]["radar_score"], expected_radar[key])
                self.assertAlmostEqual(
                    ranked[key]["total_score"],
                    expected_radar[key] * 0.8 + expected_fit[key],
                )

        self.assertEqual(result["ranked"][0]["key"], "luna|max|standard")

    def test_complete_current_radar_trio_ranks_luna_before_terra_with_equal_fit(self):
        payload = PrepareTests().payload(42.5)
        payload["available"] = [
            {"model": "gpt-5.6-sol", "effort": "xhigh"},
            {"model": "gpt-5.6-terra", "effort": "xhigh"},
            {"model": "gpt-5.6-luna", "effort": "max"},
        ]
        payload["current"] = {
            "model": "gpt-5.6-terra",
            "effort": "xhigh",
            "speed": "standard",
        }

        prepared = recommend.prepare(
            payload,
            metrics(
                [
                    point("gpt-5.6-sol", "xhigh", 102.14, 6.290296, 25.91),
                    point("gpt-5.6-terra", "xhigh", 84.64, 1.803446, 18.4),
                    point("gpt-5.6-luna", "max", 93.57, 0.474308, 32.6),
                ]
            ),
            current_radar_insights(),
            NOW,
        )

        self.assertTrue(prepared["compatibility_verified"])

        task_fit = {
            item["key"]: {
                "effort": 0,
                "workload": 0,
                "latency": 0,
                "execution_horizon": 0,
                "reason": "equal fit",
            }
            for item in prepared["candidates"]
        }

        keys = [
            item["key"]
            for item in recommend.rank_candidates(prepared, task_fit)["ranked"]
        ]

        self.assertLess(
            keys.index("luna|max|standard"), keys.index("terra|xhigh|standard")
        )

    def test_legacy_prepared_without_available_complete_is_not_rankable(self):
        prepared = self.prepared()
        del prepared["available_complete"]

        with self.assertRaisesRegex(
            ValueError, "^available model list is incomplete or unverified$"
        ):
            recommend.rank_candidates(prepared, self.fits())

    def test_fit_out_of_range_or_nonfinite_is_rejected(self):
        for invalid in (9, float("nan")):
            with self.subTest(invalid=invalid):
                fits = self.fits()
                fits["sol|xhigh|standard"]["effort"] = invalid
                with self.assertRaisesRegex(ValueError, "effort must be from 0 to 8"):
                    recommend.rank_candidates(self.prepared(), fits)

    def test_pool_outsider_is_rejected(self):
        fits = self.fits()
        fits["terra|max|standard"] = fits["luna|max|standard"]

        with self.assertRaisesRegex(
            ValueError, "task-fit keys must match candidate keys"
        ):
            recommend.rank_candidates(self.prepared(), fits)

    def test_hysteresis_keeps_qualified_current_under_five_points(self):
        prepared = self.prepared()
        prepared["current"] = {"model": "luna", "effort": "max", "speed": "standard"}

        for candidate in prepared["candidates"]:
            candidate.update(
                {
                    "iq": 100.0,
                    "price": 1.0,
                    "minutes": 10.0,
                    "samples": 420.0,
                    "age_seconds": 600.0,
                }
            )

        result = recommend.decide(prepared, self.fits(sol=14, luna=10))
        self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
        self.assertEqual(result["status"], "continue")

    def test_long_large_saving_overrides_under_five_point_hysteresis(self):
        prepared = {
            "status": "prepared",
            "risk": {"score": 60.0, "level": "L3"},
            "quality_floor": 90.0,
            "candidates": [
                {
                    "key": "terra|max|standard",
                    "model": "terra",
                    "effort": "max",
                    "speed": "standard",
                    "iq": 100.0,
                    "price": 2.0,
                    "minutes": 10.0,
                    "samples": 420.0,
                    "age_seconds": 600.0,
                    "degraded": False,
                },
                {
                    "key": "luna|max|standard",
                    "model": "luna",
                    "effort": "max",
                    "speed": "standard",
                    "iq": 94.0,
                    "price": 1.0,
                    "minutes": 30.0,
                    "samples": 420.0,
                    "age_seconds": 600.0,
                    "degraded": False,
                },
                {
                    "key": "sol|max|standard",
                    "model": "sol",
                    "effort": "max",
                    "speed": "standard",
                    "iq": 90.0,
                    "price": 3.0,
                    "minutes": 20.0,
                    "samples": 420.0,
                    "age_seconds": 600.0,
                    "degraded": False,
                },
            ],
            "current": {"model": "terra", "effort": "max", "speed": "standard"},
            "strict": False,
            "pause_on_change": True,
            "task_horizon": "long",
            "notify_on_large_savings": True,
            "available_complete": True,
            "compatibility_verified": True,
            "degradation_verified": True,
            "allow_fast": False,
            "latency_priority": "normal",
            "data": {"source": "live", "age_seconds": 0.0},
        }

        fits = {
            "terra|max|standard": {
                "effort": 8,
                "workload": 6,
                "latency": 4,
                "execution_horizon": 2,
                "reason": "deep current fit",
            },
            "luna|max|standard": {
                "effort": 4,
                "workload": 3,
                "latency": 2,
                "execution_horizon": 0,
                "reason": "cost-saving fit",
            },
            "sol|max|standard": {
                "effort": 0,
                "workload": 0,
                "latency": 0,
                "execution_horizon": 0,
                "reason": "fallback fit",
            },
        }

        ranked = recommend.rank_candidates(prepared, fits)["ranked"]
        self.assertEqual(ranked[0]["key"], "luna|max|standard")
        self.assertEqual(ranked[1]["key"], "terra|max|standard")
        self.assertGreater(ranked[0]["total_score"], ranked[1]["total_score"])
        self.assertLess(ranked[0]["total_score"] - ranked[1]["total_score"], 5.0)

        result = recommend.decide(prepared, fits)
        self.assertTrue(result["current_qualified"])
        self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
        self.assertEqual(result["status"], "warn")
        self.assertNotEqual(result["status"], "pause")
        self.assertIn("change_notice", result)
        self.assertIs(result["change_notice"]["non_blocking"], True)

    def test_cost_priority_keeps_qualified_current_luna(self):
        prepared = self.prepared()
        prepared["current"] = {"model": "luna", "effort": "max", "speed": "standard"}

        result = recommend.decide(prepared, self.fits(sol=20, luna=0))
        self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
        self.assertEqual(result["status"], "continue")

    def test_legacy_prepared_defaults_each_adaptive_notice_control(self):
        for field in ("task_horizon", "notify_on_large_savings"):
            with self.subTest(field=field):
                prepared = self.prepared()
                del prepared[field]
                result = recommend.decide(prepared, self.fits(sol=20, luna=10))
                self.assertTrue(result["current_qualified"])
                self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
                self.assertEqual(result["status"], "continue")
                self.assertIn("change_notice", result)
                self.assertIsNone(result["change_notice"])

    def test_decide_rejects_invalid_task_horizon(self):
        prepared = self.prepared()
        prepared["task_horizon"] = "medium"

        with self.assertRaisesRegex(ValueError, "^task_horizon must be short or long$"):
            recommend.decide(prepared, self.fits())

    def test_unknown_current_pauses_without_change_notice(self):
        prepared = self.prepared()
        prepared["strict"] = False
        prepared["pause_on_change"] = True
        prepared["compatibility_verified"] = True
        prepared["degradation_verified"] = True
        prepared["data"] = {"source": "live", "age_seconds": 0.0}
        prepared["current"] = None

        result = recommend.decide(prepared, self.fits(sol=20, luna=10))
        self.assertFalse(result["current_qualified"])
        self.assertEqual(result["status"], "pause")
        self.assertIn("change_notice", result)
        self.assertIsNone(result["change_notice"])

    def test_short_task_silently_continues_despite_large_qualified_savings(self):
        prepared = self.prepared()
        result = recommend.decide(prepared, self.fits(sol=20, luna=10))
        self.assertTrue(result["current_qualified"])
        self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
        self.assertEqual(result["status"], "continue")
        self.assertIn("change_notice", result)
        self.assertIsNone(result["change_notice"])

    def test_short_terra_to_luna_silently_continues_with_seventy_percent_savings(self):
        prepared = {
            "status": "prepared",
            "risk": {"score": 60.0, "level": "L3"},
            "quality_floor": 90.0,
            "candidates": [
                {
                    "key": "terra|max|standard",
                    "model": "terra",
                    "effort": "max",
                    "speed": "standard",
                    "iq": 96.0,
                    "price": 10.0,
                    "minutes": 30.0,
                    "samples": 420.0,
                    "age_seconds": 600.0,
                    "degraded": False,
                },
                {
                    "key": "luna|max|standard",
                    "model": "luna",
                    "effort": "max",
                    "speed": "standard",
                    "iq": 94.0,
                    "price": 3.0,
                    "minutes": 28.0,
                    "samples": 420.0,
                    "age_seconds": 600.0,
                    "degraded": False,
                },
            ],
            "current": {"model": "terra", "effort": "max", "speed": "standard"},
            "strict": False,
            "pause_on_change": True,
            "task_horizon": "short",
            "notify_on_large_savings": True,
            "available_complete": True,
            "compatibility_verified": True,
            "degradation_verified": True,
            "allow_fast": False,
            "latency_priority": "normal",
            "data": {"source": "live", "age_seconds": 0.0},
        }

        fits = {
            "terra|max|standard": {
                "effort": 6,
                "workload": 4,
                "latency": 3,
                "execution_horizon": 1,
                "reason": "qualified current",
            },
            "luna|max|standard": {
                "effort": 6,
                "workload": 4,
                "latency": 3,
                "execution_horizon": 1,
                "reason": "qualified savings",
            },
        }

        result = recommend.decide(prepared, fits)
        self.assertTrue(result["current_qualified"])
        self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
        self.assertEqual(result["status"], "continue")
        self.assertIn("change_notice", result)
        self.assertIsNone(result["change_notice"])

    def test_exactly_five_points_switch_continues_silently_for_short_task(self):
        prepared = self.prepared()
        prepared["current"] = {"model": "luna", "effort": "max", "speed": "standard"}

        for candidate in prepared["candidates"]:
            candidate.update(
                {
                    "iq": 100.0,
                    "price": 1.0,
                    "minutes": 10.0,
                    "samples": 420.0,
                    "age_seconds": 600.0,
                }
            )

        result = recommend.decide(prepared, self.fits(sol=15, luna=10))
        self.assertEqual(result["recommendation"]["key"], "sol|xhigh|standard")
        self.assertEqual(result["status"], "continue")
        self.assertIn("change_notice", result)
        self.assertIsNone(result["change_notice"])

    def test_long_task_warns_non_blockingly_for_at_least_fifty_percent_savings(self):
        prepared = self.prepared()
        prepared["task_horizon"] = "long"
        prepared["candidates"][1]["price"] = 3.0

        result = recommend.decide(prepared, self.fits(sol=10, luna=10))
        self.assertTrue(result["current_qualified"])
        self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
        self.assertEqual(result["status"], "warn")
        self.assertNotEqual(result["status"], "pause")
        self.assertIn("change_notice", result)
        notice = result["change_notice"]
        self.assertIsInstance(notice, dict)
        self.assertIs(notice["non_blocking"], True)
        self.assertEqual(notice["recommended_key"], "luna|max|standard")
        self.assertEqual(notice["current_price"], 6.0)
        self.assertEqual(notice["recommended_price"], 3.0)
        savings_ratio = notice["savings_ratio"]
        self.assertIsInstance(savings_ratio, (int, float))
        self.assertGreaterEqual(savings_ratio, 0.5)

    def test_strict_long_large_saving_pauses_before_nonblocking_notice(self):
        prepared = self.prepared()
        prepared["strict"] = True
        prepared["task_horizon"] = "long"
        prepared["candidates"][1]["price"] = 3.0

        result = recommend.decide(prepared, self.fits(sol=10, luna=10))
        self.assertTrue(result["current_qualified"])
        self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
        self.assertEqual(result["status"], "pause")
        self.assertIn("change_notice", result)
        notice = result["change_notice"]
        self.assertIsInstance(notice, dict)
        self.assertIs(notice["non_blocking"], True)
        self.assertEqual(notice["recommended_key"], "luna|max|standard")
        self.assertEqual(notice["current_price"], 6.0)
        self.assertEqual(notice["recommended_price"], 3.0)
        savings_ratio = notice["savings_ratio"]
        self.assertIsInstance(savings_ratio, (int, float))
        self.assertGreaterEqual(savings_ratio, 0.5)

    def test_long_unknown_current_pauses_without_change_notice_despite_apparent_savings(
        self,
    ):
        prepared = self.prepared()
        prepared["strict"] = False
        prepared["pause_on_change"] = True
        prepared["task_horizon"] = "long"
        prepared["candidates"][1]["price"] = 3.0
        prepared["compatibility_verified"] = True
        prepared["degradation_verified"] = True
        prepared["data"] = {"source": "live", "age_seconds": 0.0}
        prepared["current"] = None

        result = recommend.decide(prepared, self.fits(sol=20, luna=10))
        self.assertFalse(result["current_qualified"])
        self.assertEqual(result["status"], "pause")
        self.assertIn("change_notice", result)
        self.assertIsNone(result["change_notice"])

    def test_long_unqualified_current_pauses_without_change_notice_despite_cheaper_best(
        self,
    ):
        prepared = self.prepared()
        prepared["strict"] = False
        prepared["pause_on_change"] = True
        prepared["task_horizon"] = "long"
        prepared["candidates"][1]["price"] = 3.0
        prepared["compatibility_verified"] = True
        prepared["degradation_verified"] = True
        prepared["data"] = {"source": "live", "age_seconds": 0.0}
        prepared["current"] = {"model": "terra", "effort": "max", "speed": "standard"}

        self.assertNotIn(
            "terra|max|standard",
            {candidate["key"] for candidate in prepared["candidates"]},
        )

        fits = self.fits(sol=10, luna=10)

        self.assertEqual(
            recommend.rank_candidates(prepared, fits)["ranked"][0]["key"],
            "luna|max|standard",
        )

        result = recommend.decide(prepared, fits)
        self.assertFalse(result["current_qualified"])
        self.assertEqual(result["status"], "pause")
        self.assertIn("change_notice", result)
        self.assertIsNone(result["change_notice"])

    def test_long_task_continues_silently_below_threshold_or_when_disabled(self):
        cases = ((3.01, True, "below fifty percent"), (3.0, False, "notices disabled"))

        for candidate_price, notify_on_large_savings, label in cases:
            with self.subTest(label=label):
                prepared = self.prepared()
                prepared["task_horizon"] = "long"
                prepared["notify_on_large_savings"] = notify_on_large_savings
                prepared["candidates"][1]["price"] = candidate_price
                result = recommend.decide(prepared, self.fits(sol=10, luna=10))
                self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
                self.assertEqual(result["status"], "continue")
                self.assertIn("change_notice", result)
                self.assertIsNone(result["change_notice"])

    def test_long_task_continues_silently_without_a_notice_trigger(self):
        cases = (
            (
                "current is best",
                1.0,
                1.0,
                100.0,
                10.0,
                20,
                90.0,
                30.0,
                0,
                "terra|max|standard",
            ),
            (
                "best is not cheaper",
                2.0,
                2.0,
                90.0,
                30.0,
                0,
                100.0,
                10.0,
                20,
                "luna|max|standard",
            ),
            (
                "current price is zero",
                0.0,
                0.0,
                90.0,
                30.0,
                0,
                100.0,
                10.0,
                20,
                "luna|max|standard",
            ),
        )

        def task_fit(points, reason):
            if points == 20:
                return {
                    "effort": 8,
                    "workload": 6,
                    "latency": 4,
                    "execution_horizon": 2,
                    "reason": reason,
                }

            return {
                "effort": 0,
                "workload": 0,
                "latency": 0,
                "execution_horizon": 0,
                "reason": reason,
            }

        for (
            label,
            terra_price,
            luna_price,
            terra_iq,
            terra_minutes,
            terra_points,
            luna_iq,
            luna_minutes,
            luna_points,
            expected_key,
        ) in cases:
            with self.subTest(label=label):
                prepared = {
                    "status": "prepared",
                    "risk": {"score": 60.0, "level": "L3"},
                    "quality_floor": 90.0,
                    "candidates": [
                        {
                            "key": "terra|max|standard",
                            "model": "terra",
                            "effort": "max",
                            "speed": "standard",
                            "iq": terra_iq,
                            "price": terra_price,
                            "minutes": terra_minutes,
                            "samples": 420.0,
                            "age_seconds": 600.0,
                            "degraded": False,
                        },
                        {
                            "key": "luna|max|standard",
                            "model": "luna",
                            "effort": "max",
                            "speed": "standard",
                            "iq": luna_iq,
                            "price": luna_price,
                            "minutes": luna_minutes,
                            "samples": 420.0,
                            "age_seconds": 600.0,
                            "degraded": False,
                        },
                    ],
                    "current": {"model": "terra", "effort": "max", "speed": "standard"},
                    "strict": False,
                    "pause_on_change": True,
                    "task_horizon": "long",
                    "notify_on_large_savings": True,
                    "available_complete": True,
                    "compatibility_verified": True,
                    "degradation_verified": True,
                    "allow_fast": False,
                    "latency_priority": "normal",
                    "data": {"source": "live", "age_seconds": 0.0},
                }
                fits = {
                    "terra|max|standard": task_fit(terra_points, "qualified current"),
                    "luna|max|standard": task_fit(luna_points, "qualified candidate"),
                }
                self.assertEqual(
                    recommend.rank_candidates(prepared, fits)["ranked"][0]["key"],
                    expected_key,
                )
                result = recommend.decide(prepared, fits)
                self.assertTrue(result["current_qualified"])
                self.assertEqual(result["recommendation"]["key"], expected_key)
                self.assertEqual(result["status"], "continue")
                self.assertNotEqual(result["status"], "pause")
                self.assertIn("change_notice", result)
                self.assertIsNone(result["change_notice"])

    def test_strict_unknown_current_pauses(self):
        prepared = self.prepared()
        prepared["strict"] = True
        prepared["current"] = None

        result = recommend.decide(prepared, self.fits())
        self.assertEqual(result["status"], "pause")

    def test_strict_mode_pauses_for_material_change_from_qualified_current(self):
        prepared = self.prepared()
        prepared["strict"] = True

        result = recommend.decide(prepared, self.fits(sol=20, luna=10))
        self.assertTrue(result["current_qualified"])
        self.assertEqual(result["recommendation"]["key"], "luna|max|standard")
        self.assertEqual(result["status"], "pause")

    def test_unqualified_current_obeys_mode_and_pause_preference(self):
        prepared = self.prepared()
        prepared["current"] = {"model": "terra", "effort": "low", "speed": "standard"}
        prepared["pause_on_change"] = False

        warned = recommend.decide(prepared, self.fits())
        self.assertEqual(warned["status"], "warn")
        prepared["pause_on_change"] = True

        paused = recommend.decide(prepared, self.fits())
        self.assertEqual(paused["status"], "pause")

        for result in (warned, paused):
            with self.subTest(status=result["status"]):
                self.assertIn("change_notice", result)
                self.assertIsNone(result["change_notice"])

    def test_empty_candidate_pool_has_no_recommendation(self):
        prepared = self.prepared()
        prepared["candidates"] = []
        prepared["pause_on_change"] = False

        result = recommend.decide(prepared, {})
        self.assertEqual(result["status"], "warn")
        self.assertIsNone(result["recommendation"])
        self.assertIn("change_notice", result)
        self.assertIsNone(result["change_notice"])
        prepared["strict"] = True

        strict = recommend.decide(prepared, {})
        self.assertEqual(strict["status"], "pause")
        self.assertIn("change_notice", strict)
        self.assertIsNone(strict["change_notice"])

    def test_complete_unverified_compatibility_is_not_rankable(self):
        prepared = self.prepared()
        prepared["compatibility_verified"] = False

        with self.assertRaisesRegex(
            ValueError, "^available model list is incomplete or unverified$"
        ):
            recommend.rank_candidates(prepared, self.fits())

        with self.assertRaisesRegex(
            ValueError, "^available model list is incomplete or unverified$"
        ):
            recommend.run(
                {
                    "action": "rank",
                    "prepared": prepared,
                    "task_fit_by_candidate": self.fits(),
                }
            )

    def test_verified_live_and_cache_confidence_levels(self):
        prepared = self.prepared()
        prepared["data"] = {"source": "live", "age_seconds": 0.0}

        self.assertEqual(recommend.decide(prepared, self.fits())["confidence"], "high")
        prepared["data"] = {"source": "cache", "age_seconds": 1800.0}

        self.assertEqual(
            recommend.decide(prepared, self.fits())["confidence"], "medium"
        )

    def test_percentile_rejects_malformed_arguments(self):
        cases = (
            ({"not": "a sequence"}, True),
            ([1, True], True),
            ([1, float("nan")], True),
            ([1, float("inf")], True),
            ([1], "yes"),
        )

        for values, higher_better in cases:
            with self.subTest(values=values, higher_better=higher_better):
                with self.assertRaisesRegex(ValueError, "percentile"):
                    recommend.percentile_scores(values, higher_better)

    def test_rank_rejects_malformed_prepared_and_candidates(self):
        with self.assertRaisesRegex(ValueError, "prepared"):
            recommend.rank_candidates(None, {})

        for prepared in (
            None,
            {"status": "pause"},
            {"status": "warn"},
            {"status": "no_candidates"},
        ):
            with self.subTest(run_prepared=prepared):
                result = recommend.run(
                    {
                        "action": "rank",
                        "prepared": prepared,
                        "task_fit_by_candidate": {},
                    }
                )
                self.assertEqual(result["status"], "pause")
                self.assertEqual(result["error"], "prepared_not_rankable")
        prepared = self.prepared()
        prepared["candidates"] = {}

        with self.assertRaisesRegex(ValueError, "candidates"):
            recommend.rank_candidates(prepared, {})
        prepared = self.prepared()
        prepared["candidates"][0] = []

        with self.assertRaisesRegex(ValueError, "candidate"):
            recommend.rank_candidates(prepared, self.fits())
        prepared = self.prepared()
        prepared["candidates"].append(dict(prepared["candidates"][0]))

        with self.assertRaisesRegex(ValueError, "unique"):
            recommend.rank_candidates(prepared, self.fits())
        prepared = self.prepared()
        prepared["candidates"][0]["key"] = "sol|wrong|standard"

        with self.assertRaisesRegex(ValueError, "key"):
            recommend.rank_candidates(prepared, self.fits())

    def test_rank_rejects_invalid_candidate_metrics(self):
        cases = (
            ("iq", float("nan")),
            ("price", float("inf")),
            ("minutes", True),
            ("samples", 0),
            ("age_seconds", -1),
        )

        for field, value in cases:
            with self.subTest(field=field, value=value):
                prepared = self.prepared()
                prepared["candidates"][0][field] = value
                with self.assertRaisesRegex(ValueError, f"candidate {field}"):
                    recommend.rank_candidates(prepared, self.fits())

    def test_rank_rejects_malformed_task_fit(self):
        with self.assertRaisesRegex(ValueError, "task-fit"):
            recommend.rank_candidates(self.prepared(), [])
        fits = self.fits()
        fits["sol|xhigh|standard"] = []

        with self.assertRaisesRegex(ValueError, "task-fit entry"):
            recommend.rank_candidates(self.prepared(), fits)
        fits = self.fits()
        fits["sol|xhigh|standard"]["reason"] = " "

        with self.assertRaisesRegex(ValueError, "reason"):
            recommend.rank_candidates(self.prepared(), fits)

    def test_rank_requires_boolean_control_fields(self):
        for field in (
            "strict",
            "pause_on_change",
            "notify_on_large_savings",
            "available_complete",
            "compatibility_verified",
            "degradation_verified",
            "allow_fast",
            "luna_max_fast_preference",
        ):
            with self.subTest(field=field):
                prepared = self.prepared()
                prepared[field] = 1
                with self.assertRaisesRegex(ValueError, field):
                    recommend.rank_candidates(prepared, self.fits())

    def test_four_candidate_scores_preserve_exact_80_20_equation(self):
        prepared = self.prepared()

        prepared["candidates"].extend(
            [
                {
                    "key": "terra|high|standard",
                    "model": "terra",
                    "effort": "high",
                    "speed": "standard",
                    "iq": 101.0,
                    "price": 2.0,
                    "minutes": 18.0,
                    "samples": 300.0,
                    "age_seconds": 3600.0,
                    "degraded": False,
                },
                {
                    "key": "alpha|max|standard",
                    "model": "alpha",
                    "effort": "max",
                    "speed": "standard",
                    "iq": 97.0,
                    "price": 3.0,
                    "minutes": 14.0,
                    "samples": 350.0,
                    "age_seconds": 7200.0,
                    "degraded": False,
                },
            ]
        )

        fits = self.fits()

        fits.update(
            {
                "terra|high|standard": {
                    "effort": 5,
                    "workload": 4,
                    "latency": 3,
                    "execution_horizon": 1,
                    "reason": "balanced",
                },
                "alpha|max|standard": {
                    "effort": 6,
                    "workload": 3,
                    "latency": 2,
                    "execution_horizon": 1,
                    "reason": "quick",
                },
            }
        )

        for item in recommend.rank_candidates(prepared, fits)["ranked"]:
            self.assertEqual(
                item["total_score"], item["radar_score"] * 0.8 + item["task_fit_points"]
            )

    def test_confidence_is_low_for_malformed_or_unknown_data(self):
        self.assertEqual(recommend.confidence_for(None), "low")
        prepared = self.prepared()

        for data in (None, {}, {"source": "other"}, "live"):
            with self.subTest(data=data):
                prepared["data"] = data
                self.assertEqual(recommend.confidence_for(prepared), "low")

    def test_confidence_requires_fresh_finite_data_age(self):
        prepared = self.prepared()

        for source in ("live", "cache"):
            for age in (
                None,
                -1,
                True,
                float("nan"),
                float("inf"),
                (recommend.MAX_DATA_AGE_SECONDS) + 1,
            ):
                with self.subTest(source=source, age=age):
                    prepared["data"] = {"source": source, "age_seconds": age}
                    self.assertEqual(recommend.confidence_for(prepared), "low")
        prepared["data"] = {"source": "live", "age_seconds": 0.0}

        self.assertEqual(recommend.confidence_for(prepared), "high")
        prepared["data"] = {
            "source": "cache",
            "age_seconds": recommend.MAX_DATA_AGE_SECONDS,
        }

        self.assertEqual(recommend.confidence_for(prepared), "medium")

    def test_decide_validates_current_configuration(self):
        prepared = self.prepared()
        prepared["current"] = []

        with self.assertRaisesRegex(ValueError, "current"):
            recommend.decide(prepared, self.fits())
        prepared = self.prepared()
        prepared["current"] = {"model": "sol", "effort": "xhigh", "speed": ""}

        with self.assertRaisesRegex(ValueError, "current"):
            recommend.decide(prepared, self.fits())
        prepared = self.prepared()
        prepared["current"] = {"model": "sol", "effort": "xhigh"}

        with self.assertRaisesRegex(ValueError, "current"):
            recommend.decide(prepared, self.fits())

    def test_qualified_speed_only_change_continues_silently_for_short_task(self):
        prepared = self.prepared()
        prepared["allow_fast"] = True
        prepared["latency_priority"] = "high"
        prepared["current"] = {"model": "sol", "effort": "xhigh", "speed": "standard"}

        fits = self.fits()
        for candidate in prepared["candidates"]:
            old_key = candidate["key"]
            candidate["speed"] = "fast"
            candidate["key"] = recommend.candidate_key(
                candidate["model"], candidate["effort"], "fast"
            )
            fits[candidate["key"]] = fits.pop(old_key)
        result = recommend.decide(prepared, fits)
        self.assertTrue(result["current_qualified"])
        self.assertEqual(result["recommendation"]["speed"], "fast")
        self.assertEqual(result["status"], "continue")
        self.assertIn("change_notice", result)
        self.assertIsNone(result["change_notice"])
        self.assertNotIn(
            "current configuration failed the quality gate", result["reasons"]
        )

    def test_rank_rejects_fast_candidate_without_authorization(self):
        prepared = self.prepared()
        candidate = prepared["candidates"][0]
        old_key = candidate["key"]
        candidate["speed"] = "fast"
        candidate["key"] = recommend.candidate_key(
            candidate["model"], candidate["effort"], "fast"
        )

        fits = self.fits()
        fits[candidate["key"]] = fits.pop(old_key)

        with self.assertRaisesRegex(ValueError, "^candidate speed is not authorized$"):
            recommend.rank_candidates(prepared, fits)

    def test_empty_pool_pauses_when_pause_on_change_is_enabled(self):
        prepared = self.prepared()
        prepared["candidates"] = []
        prepared["pause_on_change"] = True

        result = recommend.decide(prepared, {})
        self.assertEqual(result["status"], "pause")
        self.assertIn("change_notice", result)
        self.assertIsNone(result["change_notice"])

    def test_empty_pool_still_validates_current_configuration(self):
        prepared = self.prepared()
        prepared["candidates"] = []
        prepared["current"] = {"model": "sol", "effort": "xhigh"}

        with self.assertRaisesRegex(ValueError, "current"):
            recommend.decide(prepared, {})

    def test_low_confidence_never_recommends_a_qualified_current_candidate(self):
        prepared = self.prepared()
        prepared["current"] = {"model": "luna", "effort": "max", "speed": "standard"}
        prepared["pause_on_change"] = False
        prepared["data"] = {"source": "live", "age_seconds": float("nan")}

        result = recommend.decide(prepared, self.fits(sol=20, luna=0))
        self.assertEqual(result["confidence"], "low")
        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["status"], "warn")
        self.assertIn("recommendation data is insufficient", result["reasons"])

    def test_low_confidence_does_not_switch_for_speed_only_change(self):
        prepared = self.prepared()
        prepared["allow_fast"] = True
        prepared["latency_priority"] = "high"
        prepared["current"] = {"model": "sol", "effort": "xhigh", "speed": "standard"}
        prepared["pause_on_change"] = False
        prepared["data"] = None

        fits = self.fits()
        for candidate in prepared["candidates"]:
            old_key = candidate["key"]
            candidate["speed"] = "fast"
            candidate["key"] = recommend.candidate_key(
                candidate["model"], candidate["effort"], "fast"
            )
            fits[candidate["key"]] = fits.pop(old_key)
        result = recommend.decide(prepared, fits)
        self.assertTrue(result["current_qualified"])
        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["status"], "warn")

    def test_low_confidence_does_not_recommend_without_current_candidate(self):
        prepared = self.prepared()
        prepared["current"] = None
        prepared["pause_on_change"] = False
        prepared["data"] = {
            "source": "cache",
            "age_seconds": (recommend.MAX_DATA_AGE_SECONDS) + 1,
        }

        result = recommend.decide(prepared, self.fits(sol=20, luna=0))
        self.assertEqual(result["recommendation"], None)
        self.assertEqual(result["status"], "warn")
        self.assertIn("recommendation data is insufficient", result["reasons"])
        prepared["pause_on_change"] = True

        result = recommend.decide(prepared, self.fits(sol=20, luna=0))
        self.assertEqual(result["recommendation"], None)
        self.assertEqual(result["status"], "pause")

    def test_strict_low_confidence_pauses_without_recommendation(self):
        prepared = self.prepared()
        prepared["strict"] = True
        prepared["pause_on_change"] = False
        prepared["current"] = {"model": "luna", "effort": "max", "speed": "standard"}
        prepared["data"] = None

        result = recommend.decide(prepared, self.fits(sol=20, luna=0))
        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["status"], "pause")
        self.assertIn("recommendation data is insufficient", result["reasons"])


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.metrics = metrics([point("sol", "xhigh", 109, 6, 26)])
        self.insights = insights()

    def test_live_data_is_cached_without_private_input(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "radar.json"
            responses = {
                recommend.METRICS_URL: self.metrics,
                recommend.INSIGHTS_URL: self.insights,
            }
            fetcher = lambda url: responses[url]
            data = recommend.load_radar_data(cache, NOW, fetcher)
            self.assertEqual(data["source"], "live")
            saved = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"metrics", "saved_at", "insights"})
            self.assertFalse(cache.with_suffix((cache.suffix) + ".tmp").exists())
            staged = []

            def replace(source, _destination):
                staged.append(Path(source).name)
                raise OSError("replace failed")

            with mock.patch.object(recommend.os, "replace", side_effect=replace):
                for _ in range(2):
                    with self.assertRaisesRegex(OSError, "replace failed"):
                        recommend._write_cache(cache, NOW, self.metrics, self.insights)
            self.assertEqual(len(staged), 2)
            self.assertNotEqual(*staged)
            self.assertEqual(list(Path(directory).glob("radar.json.*")), [])

    def test_valid_cache_is_used_after_live_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "radar.json"
            cache.write_text(
                json.dumps(
                    {
                        "saved_at": "2026-08-09T13:30:00+00:00",
                        "metrics": self.metrics,
                        "insights": self.insights,
                    }
                ),
                encoding="utf-8",
            )

            def fail(_url):
                raise http.client.IncompleteRead("partial")

            data = recommend.load_radar_data(str(cache), NOW, fail)
            self.assertEqual(data["source"], "cache")
            cache.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "no valid live or cached Radar data"
            ):
                recommend.load_radar_data(cache, NOW, fail)

        with self.assertRaisesRegex(ValueError, "cache_path must be a path"):
            recommend.load_radar_data(object(), NOW, fail)

        with self.assertRaisesRegex(
            ValueError, "now must be a timezone-aware datetime"
        ):
            recommend.load_radar_data(cache, "now", fail)

    def test_expired_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "radar.json"
            cache.write_text(
                json.dumps(
                    {
                        "saved_at": "2026-08-08T10:00:00+00:00",
                        "metrics": self.metrics,
                        "insights": self.insights,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "no valid live or cached Radar data"
            ):
                fetcher = lambda _url: (_ for _ in ()).throw(OSError("offline"))
                recommend.load_radar_data(cache, NOW, fetcher)

    def test_stale_live_payload_falls_back_without_overwriting_valid_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "radar.json"
            cache.write_text(
                json.dumps(
                    {
                        "saved_at": "2026-08-09T13:30:00+00:00",
                        "metrics": self.metrics,
                        "insights": self.insights,
                    }
                ),
                encoding="utf-8",
            )
            stale_metrics = metrics([point("sol", "xhigh", 109, 6, 26)])
            stale_metrics["schema"] = 2.0
            responses = {
                recommend.METRICS_URL: stale_metrics,
                recommend.INSIGHTS_URL: self.insights,
            }
            fetcher = lambda url: responses[url]
            data = recommend.load_radar_data(cache, NOW, fetcher)
            self.assertEqual(data["source"], "cache")
            saved = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["metrics"]["source_updated_at"], self.metrics["source_updated_at"]
            )

    def test_prepare_action_returns_structured_data_insufficient(self):
        payload = {
            "action": "prepare",
            "risk_dimensions": {name: 0 for name in recommend.RISK_WEIGHTS},
            "force_l4": False,
            "current": None,
            "available": None,
            "strict": True,
            "pause_on_change": True,
            "latency_priority": "normal",
            "allow_fast": False,
        }

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-cache.json"
            result = recommend.run(
                payload,
                cache_path=missing,
                now=NOW,
                fetcher=(
                    lambda _url: (_ for _ in ()).throw(
                        http.client.IncompleteRead("partial")
                    )
                ),
            )

        self.assertEqual(result["status"], "pause")
        self.assertEqual(result["error"], "data_insufficient")
        self.assertEqual(result["confidence"], "low")
        payload["strict"] = False

        result = recommend.run(
            payload,
            cache_path=missing,
            now=NOW,
            fetcher=(lambda _url: (_ for _ in ()).throw(OSError("offline"))),
        )

        self.assertEqual(result["status"], "pause")

        for field, value, message in (
            ("strict", "false", "strict must be a boolean"),
            ("strict", 0, "strict must be a boolean"),
            ("pause_on_change", 0, "pause_on_change must be a boolean"),
            ("allow_fast", "false", "allow_fast must be a boolean"),
            ("force_l4", "false", "force_l4 must be a boolean"),
            (
                "latency_priority",
                float("nan"),
                "latency_priority must be normal or high",
            ),
        ):
            with self.subTest(field=field, value=value):
                invalid = dict(payload)
                invalid[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    recommend.run(
                        invalid,
                        cache_path=missing,
                        now=NOW,
                        fetcher=(
                            lambda _url: (_ for _ in ()).throw(
                                AssertionError("validation must precede fetching")
                            )
                        ),
                    )

        output = io.StringIO()

        with mock.patch.object(recommend.sys, "stdin", io.StringIO('{"action": NaN}')):
            with mock.patch.object(recommend.sys, "stdout", output):
                self.assertEqual(recommend.main(), 2)
        error = json.loads(output.getvalue())
        self.assertEqual(error["status"], "error")
        self.assertIn("invalid JSON constant", error["error"])
        self.assertNotIn("NaN", output.getvalue())

    def test_prepare_action_defaults_adaptive_notice_controls(self):
        payload = {
            "action": "prepare",
            "risk_dimensions": {name: 0 for name in recommend.RISK_WEIGHTS},
            "force_l4": False,
            "current": {"model": "sol", "effort": "xhigh", "speed": "standard"},
            "available": [{"model": "sol", "effort": "xhigh"}],
            "available_complete": True,
            "strict": False,
            "pause_on_change": False,
            "latency_priority": "normal",
            "allow_fast": False,
        }

        responses = {
            recommend.METRICS_URL: self.metrics,
            recommend.INSIGHTS_URL: self.insights,
        }

        with tempfile.TemporaryDirectory() as directory:
            result = recommend.run(
                payload,
                cache_path=Path(directory) / "radar.json",
                now=NOW,
                fetcher=(lambda url: responses[url]),
            )

        self.assertEqual(result.get("task_horizon"), "short")
        self.assertIs(result.get("notify_on_large_savings"), True)
        self.assertEqual(result["status"], "prepared")
        self.assertTrue(result["compatibility_verified"])

    def test_prepare_action_defaults_each_adaptive_notice_control_independently(self):
        payload = {
            "action": "prepare",
            "risk_dimensions": {name: 0 for name in recommend.RISK_WEIGHTS},
            "force_l4": False,
            "current": {"model": "sol", "effort": "xhigh", "speed": "standard"},
            "available": [{"model": "sol", "effort": "xhigh"}],
            "available_complete": True,
            "strict": False,
            "pause_on_change": False,
            "latency_priority": "normal",
            "allow_fast": False,
        }

        responses = {
            recommend.METRICS_URL: self.metrics,
            recommend.INSIGHTS_URL: self.insights,
        }

        cases = (
            (
                "long horizon defaults notices on",
                "notify_on_large_savings",
                {"task_horizon": "long"},
                "long",
                True,
            ),
            (
                "disabled notices default horizon short",
                "task_horizon",
                {"notify_on_large_savings": False},
                "short",
                False,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            for index, (
                label,
                missing_field,
                explicit,
                expected_horizon,
                expected_notify,
            ) in enumerate(cases):
                with self.subTest(label=label):
                    request = dict(payload)
                    request.update(explicit)
                    self.assertNotIn(missing_field, request)
                    result = recommend.run(
                        request,
                        cache_path=Path(directory) / f"{index}.json",
                        now=NOW,
                        fetcher=(lambda url: responses[url]),
                    )
                    self.assertEqual(result.get("task_horizon"), expected_horizon)
                    self.assertIs(
                        result.get("notify_on_large_savings"), expected_notify
                    )
                    self.assertEqual(result["status"], "prepared")
                    self.assertTrue(result["compatibility_verified"])

    def test_prepare_action_validates_adaptive_notice_controls_before_fetching(self):
        payload = {
            "action": "prepare",
            "risk_dimensions": {name: 0 for name in recommend.RISK_WEIGHTS},
            "force_l4": False,
            "current": {"model": "sol", "effort": "xhigh", "speed": "standard"},
            "available": [{"model": "sol", "effort": "xhigh"}],
            "available_complete": True,
            "strict": False,
            "pause_on_change": False,
            "latency_priority": "normal",
            "allow_fast": False,
        }

        responses = {
            recommend.METRICS_URL: self.metrics,
            recommend.INSIGHTS_URL: self.insights,
        }

        cases = (
            ("task_horizon", None, "task_horizon must be short or long"),
            ("task_horizon", "medium", "task_horizon must be short or long"),
            ("task_horizon", True, "task_horizon must be short or long"),
            (
                "notify_on_large_savings",
                None,
                "notify_on_large_savings must be a boolean",
            ),
            ("notify_on_large_savings", 0, "notify_on_large_savings must be a boolean"),
            (
                "notify_on_large_savings",
                "true",
                "notify_on_large_savings must be a boolean",
            ),
            ("available_complete", None, "available_complete must be a boolean"),
            ("available_complete", 0, "available_complete must be a boolean"),
            ("available_complete", 1, "available_complete must be a boolean"),
            ("available_complete", "true", "available_complete must be a boolean"),
        )

        with tempfile.TemporaryDirectory() as directory:
            for index, (field, value, message) in enumerate(cases):
                with self.subTest(field=field, value=value):
                    invalid = dict(payload)
                    invalid[field] = value
                    calls = []

                    def fetcher(url):
                        calls.append(url)
                        return responses[url]

                    with self.assertRaisesRegex(ValueError, f"^{message}$"):
                        recommend.run(
                            invalid,
                            cache_path=Path(directory) / f"{index}.json",
                            now=NOW,
                            fetcher=fetcher,
                        )
                self.assertEqual(calls, [])

    def test_complete_available_marker_requires_a_valid_list_before_fetching(self):
        payload = {
            "action": "prepare",
            "risk_dimensions": {name: 0 for name in recommend.RISK_WEIGHTS},
            "force_l4": False,
            "current": {"model": "sol", "effort": "xhigh", "speed": "standard"},
            "available_complete": True,
            "strict": False,
            "pause_on_change": False,
            "latency_priority": "normal",
            "allow_fast": False,
        }

        with tempfile.TemporaryDirectory() as directory:
            cases = (
                ("missing", None),
                ("none", None),
                ("malformed", [{"model": "sol"}]),
            )
            for index, (label, available) in enumerate(cases):
                with self.subTest(label=label):
                    invalid = dict(payload)
                    if label != "missing":
                        invalid["available"] = available
                    calls = []

                    def fetcher(url):
                        calls.append(url)
                        raise AssertionError("validation must precede fetching")

                    with self.assertRaisesRegex(
                        ValueError,
                        "^available must be a valid list when available_complete is true$",
                    ):
                        recommend.run(
                            invalid,
                            cache_path=Path(directory) / f"{index}.json",
                            now=NOW,
                            fetcher=fetcher,
                        )
                self.assertEqual(calls, [])

    def test_strict_unverified_compatibility_returns_early_low_confidence(self):
        payload = {
            "action": "prepare",
            "risk_dimensions": {name: 0 for name in recommend.RISK_WEIGHTS},
            "force_l4": False,
            "current": None,
            "available": None,
            "strict": True,
            "pause_on_change": True,
            "latency_priority": "normal",
            "allow_fast": False,
        }

        responses = {
            recommend.METRICS_URL: self.metrics,
            recommend.INSIGHTS_URL: self.insights,
        }

        with tempfile.TemporaryDirectory() as directory:
            result = recommend.run(
                payload,
                cache_path=Path(directory) / "radar.json",
                now=NOW,
                fetcher=(lambda url: responses[url]),
            )

        self.assertEqual(result["status"], "pause")
        self.assertEqual(result["confidence"], "low")
        verified = dict(payload)

        verified.update(
            {
                "strict": False,
                "pause_on_change": False,
                "current": {
                    "model": "sol",
                    "effort": "xhigh",
                    "speed": "standard",
                    "opaque": float("nan"),
                },
                "available": [{"model": "sol", "effort": "xhigh"}],
                "available_complete": True,
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            prepared = recommend.run(
                verified,
                cache_path=Path(directory) / "radar.json",
                now=NOW,
                fetcher=(lambda url: responses[url]),
            )

        self.assertEqual(
            prepared["current"],
            {"model": "sol", "effort": "xhigh", "speed": "standard"},
        )

        self.assertEqual(prepared["status"], "prepared")
        self.assertTrue(prepared["compatibility_verified"])
        empty = dict(verified)
        empty["pause_on_change"] = True

        no_candidates = metrics([point("sol", "xhigh", 1, 6, 26)])

        empty_responses = {
            recommend.METRICS_URL: no_candidates,
            recommend.INSIGHTS_URL: self.insights,
        }

        with tempfile.TemporaryDirectory() as directory:
            result = recommend.run(
                empty,
                cache_path=Path(directory) / "radar.json",
                now=NOW,
                fetcher=(lambda url: empty_responses[url]),
            )

        self.assertEqual(result["status"], "pause")


class PersistentLunaPreferenceTests(unittest.TestCase):
    def test_prepare_luna_preference_limits_luna_to_max_fast_and_keeps_others_standard(
        self,
    ):
        payload = PrepareTests().payload(60)
        payload["luna_max_fast_preference"] = True
        payload["available"] = [
            {"model": "sol", "effort": "xhigh"},
            {"model": "terra", "effort": "max"},
            {"model": "luna", "effort": "high"},
            {"model": "luna", "effort": "xhigh"},
            {"model": "luna", "effort": "max"},
        ]

        prepared = recommend.prepare(
            payload,
            metrics(
                [
                    point("sol", "xhigh", 101, 6, 26),
                    point("terra", "max", 96, 2, 20),
                    point("luna", "high", 98, 0.5, 31),
                    point("luna", "xhigh", 97, 0.5, 32),
                    point("luna", "max", 95, 0.5, 33),
                ]
            ),
            current_radar_insights(),
            NOW,
        )

        by_pair = {
            (item["model"], item["effort"]): item for item in prepared["candidates"]
        }

        self.assertTrue(prepared["luna_max_fast_preference"])
        self.assertEqual(
            set(by_pair), {("terra", "max"), ("sol", "xhigh"), ("luna", "max")}
        )

        self.assertEqual(by_pair[("luna", "max")]["speed"], "fast")
        self.assertEqual(by_pair[("sol", "xhigh")]["speed"], "standard")
        self.assertEqual(by_pair[("terra", "max")]["speed"], "standard")

        self.assertEqual(
            {
                (item["model"], item["effort"])
                for item in prepared["preference_excluded_quality_pairs"]
            },
            {("luna", "high"), ("luna", "xhigh")},
        )

    def test_luna_preference_is_opt_in_and_must_be_boolean(self):
        payload = PrepareTests().payload(60)
        payload["available"].append({"model": "luna", "effort": "max"})

        prepared = recommend.prepare(
            payload,
            metrics([point("luna", "max", 95, 0.5, 33)]),
            current_radar_insights(),
            NOW,
        )

        self.assertEqual(prepared["candidates"][0]["speed"], "standard")
        self.assertFalse(prepared["luna_max_fast_preference"])

        for invalid in (0, 1, "true"):
            with self.subTest(invalid=invalid):
                invalid_payload = PrepareTests().payload(60)
                invalid_payload["luna_max_fast_preference"] = invalid
                with self.assertRaisesRegex(
                    ValueError, "^luna_max_fast_preference must be a boolean$"
                ):
                    recommend.prepare(
                        invalid_payload,
                        metrics([point("sol", "high", 93, 4, 20)]),
                        insights(),
                        NOW,
                    )

    def test_rank_rejects_luna_nonmax_or_standard_bypasses_when_preference_is_enabled(
        self,
    ):
        standard_luna = RankingTests().prepared()
        standard_luna["luna_max_fast_preference"] = True

        with self.assertRaisesRegex(ValueError, "^candidate speed is not authorized$"):
            recommend.rank_candidates(standard_luna, RankingTests().fits())

        nonmax_luna = RankingTests().prepared()
        nonmax_luna["luna_max_fast_preference"] = True

        candidate = nonmax_luna["candidates"][1]

        candidate.update({"key": "luna|xhigh|fast", "effort": "xhigh", "speed": "fast"})

        fits = RankingTests().fits()
        fits["luna|xhigh|fast"] = fits.pop("luna|max|standard")

        with self.assertRaisesRegex(ValueError, "^candidate effort is not authorized$"):
            recommend.rank_candidates(nonmax_luna, fits)

    def test_quality_qualified_current_luna_nonmax_does_not_pause_only_for_preference_filter(
        self,
    ):
        payload = PrepareTests().payload(60)

        payload.update(
            {
                "luna_max_fast_preference": True,
                "current": {"model": "luna", "effort": "xhigh", "speed": "standard"},
                "available": [
                    {"model": "luna", "effort": "xhigh"},
                    {"model": "luna", "effort": "max"},
                ],
            }
        )

        prepared = recommend.prepare(
            payload,
            metrics(
                [point("luna", "xhigh", 97, 0.5, 32), point("luna", "max", 95, 0.5, 33)]
            ),
            current_radar_insights(),
            NOW,
        )
        prepared["data"] = {"source": "live", "age_seconds": 0.0}

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]], ["luna|max|fast"]
        )

        result = recommend.decide(
            prepared,
            {
                "luna|max|fast": {
                    "effort": 0,
                    "workload": 0,
                    "latency": 0,
                    "execution_horizon": 0,
                    "reason": "persistent preference",
                }
            },
        )

        self.assertTrue(result["current_qualified"])
        self.assertEqual(result["recommendation"]["key"], "luna|max|fast")

        self.assertEqual(result["speed_source"], "persistent_luna_max_fast_preference")

        self.assertEqual(result["status"], "continue")
        self.assertNotIn(
            "current configuration failed the quality gate", result["reasons"]
        )

    def test_preference_filtering_the_only_luna_candidate_keeps_a_qualified_current_short_task_running(
        self,
    ):
        payload = PrepareTests().payload(60)

        payload.update(
            {
                "luna_max_fast_preference": True,
                "current": {"model": "luna", "effort": "xhigh", "speed": "standard"},
                "available": [{"model": "luna", "effort": "xhigh"}],
            }
        )

        prepared = recommend.prepare(
            payload,
            metrics([point("luna", "xhigh", 97, 0.5, 32)]),
            current_radar_insights(),
            NOW,
        )
        prepared["data"] = {"source": "live", "age_seconds": 0.0}

        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["candidates"], [])
        result = recommend.decide(prepared, {})
        self.assertTrue(result["current_qualified"])
        self.assertIsNone(result["recommendation"])
        self.assertEqual(result["status"], "continue")

    def test_public_prepare_preserves_the_persistent_luna_preference(self):
        payload = PrepareTests().payload(60)

        payload.update(
            {
                "action": "prepare",
                "luna_max_fast_preference": True,
                "available": [
                    {"model": "sol", "effort": "xhigh"},
                    {"model": "luna", "effort": "max"},
                ],
            }
        )

        responses = {
            recommend.METRICS_URL: metrics(
                [point("sol", "xhigh", 101, 6, 26), point("luna", "max", 95, 0.5, 33)]
            ),
            recommend.INSIGHTS_URL: current_radar_insights(),
        }

        with tempfile.TemporaryDirectory() as directory:
            result = recommend.run(
                payload,
                cache_path=Path(directory) / "radar.json",
                now=NOW,
                fetcher=(lambda url: responses[url]),
            )

        self.assertEqual(result["status"], "prepared")
        self.assertTrue(result["luna_max_fast_preference"])

        self.assertEqual(
            {item["key"] for item in result["candidates"]},
            {"sol|xhigh|standard", "luna|max|fast"},
        )


class LunaQualityBaselineTests(unittest.TestCase):
    def payload(self, strict=False):
        payload = PrepareTests().payload(0, strict=strict)

        payload.update(
            {
                "luna_quality_baseline": True,
                "available": [
                    {"model": "terra", "effort": "medium"},
                    {"model": "sol", "effort": "high"},
                    {"model": "luna", "effort": "max"},
                ],
                "current": {"model": "sol", "effort": "high", "speed": "standard"},
            }
        )
        return payload

    def baseline_metrics(self):
        return metrics(
            [
                point("terra", "medium", 57.5, 0.620576, 8.97),
                point("sol", "high", 98.93, 4.621715, 20.96),
                point("luna", "max", 93.93, 0.475231, 32.72),
            ]
        )

    def test_luna_max_standard_iq_raises_the_floor_and_filters_lower_iq_candidates(
        self,
    ):
        payload = self.payload()
        payload["luna_max_fast_preference"] = True

        prepared = recommend.prepare(
            payload, self.baseline_metrics(), current_radar_insights(), NOW
        )

        self.assertIn("luna_quality_baseline", prepared)
        self.assertTrue(prepared["luna_quality_baseline"])
        self.assertEqual(prepared["base_quality_floor"], 55.0)
        self.assertEqual(prepared["luna_quality_baseline_iq"], 93.93)
        self.assertEqual(prepared["quality_floor"], 93.93)
        self.assertEqual(prepared["comparison_speed"], "standard")

        self.assertEqual(
            [item["key"] for item in prepared["candidates"]],
            ["sol|high|standard", "luna|max|fast"],
        )

        fit = {
            item["key"]: {
                "effort": 0,
                "workload": 0,
                "latency": 0,
                "execution_horizon": 0,
                "reason": "standard comparison",
            }
            for item in prepared["candidates"]
        }

        ranked = recommend.rank_candidates(prepared, fit)["ranked"]
        self.assertEqual(ranked[0]["key"], "luna|max|fast")
        self.assertEqual(prepared["comparison_speed"], "standard")

    def test_unavailable_luna_max_baseline_never_falls_back_to_the_l1_floor(self):
        for strict, expected_status in ((False, "warn"), (True, "pause")):
            with self.subTest(strict=strict):
                payload = self.payload(strict=strict)
                payload["available"] = [{"model": "sol", "effort": "high"}]
                prepared = recommend.prepare(
                    payload,
                    metrics([point("sol", "high", 98.93, 4.621715, 20.96)]),
                    current_radar_insights(),
                    NOW,
                )
                self.assertEqual(prepared["status"], expected_status)
                self.assertEqual(prepared["base_quality_floor"], 55.0)
                self.assertIsNone(prepared["luna_quality_baseline_iq"])
                self.assertIsNone(prepared["quality_floor"])
                self.assertEqual(prepared["candidates"], [])
                self.assertIn(
                    "Luna max quality baseline is unavailable", prepared["reasons"]
                )

    def test_quality_baseline_and_comparison_speed_controls_are_strictly_validated(
        self,
    ):
        for invalid in (0, 1, "true"):
            with self.subTest(control="luna_quality_baseline", invalid=invalid):
                payload = self.payload()
                payload["luna_quality_baseline"] = invalid
                with self.assertRaisesRegex(
                    ValueError, "^luna_quality_baseline must be a boolean$"
                ):
                    recommend.prepare(
                        payload, self.baseline_metrics(), current_radar_insights(), NOW
                    )
        payload = self.payload()
        payload["comparison_speed"] = "fast"

        with self.assertRaisesRegex(ValueError, "^comparison_speed must be standard$"):
            recommend.prepare(
                payload, self.baseline_metrics(), current_radar_insights(), NOW
            )

    def test_direct_rank_rejects_a_candidate_below_the_active_luna_quality_floor(self):
        prepared = RankingTests().prepared()

        prepared.update(
            {
                "base_quality_floor": 90.0,
                "luna_quality_baseline": True,
                "luna_quality_baseline_iq": 94.0,
                "quality_floor": 94.0,
                "comparison_speed": "standard",
            }
        )
        prepared["candidates"][1]["iq"] = 93.99

        with self.assertRaisesRegex(
            ValueError, "^candidate iq is below quality floor$"
        ):
            recommend.rank_candidates(prepared, RankingTests().fits())

    def test_direct_rank_rejects_fast_as_a_comparison_speed(self):
        prepared = RankingTests().prepared()
        prepared["comparison_speed"] = "fast"

        with self.assertRaisesRegex(ValueError, "^comparison_speed must be standard$"):
            recommend.rank_candidates(prepared, RankingTests().fits())

    def test_qualified_short_task_continues_when_the_luna_baseline_changes_recommendation(
        self,
    ):
        payload = self.payload()
        payload["luna_max_fast_preference"] = True

        prepared = recommend.prepare(
            payload, self.baseline_metrics(), current_radar_insights(), NOW
        )
        prepared["data"] = {"source": "live", "age_seconds": 0.0}

        fit = {
            item["key"]: {
                "effort": 0,
                "workload": 0,
                "latency": 0,
                "execution_horizon": 0,
                "reason": "same task fit",
            }
            for item in prepared["candidates"]
        }

        result = recommend.decide(prepared, fit)
        self.assertTrue(result["current_qualified"])
        self.assertEqual(result["recommendation"]["key"], "luna|max|fast")
        self.assertEqual(result["status"], "continue")


class SpeedTests(unittest.TestCase):
    def test_fast_requires_both_latency_priority_and_permission(self):
        self.assertEqual(recommend.choose_speed("high", False), "standard")
        self.assertEqual(recommend.choose_speed("normal", True), "standard")
        self.assertEqual(recommend.choose_speed("high", True), "fast")

    def test_fast_permission_must_be_a_boolean(self):
        for invalid in (0, 1, "true"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ValueError, "^allow_fast must be a boolean$"
                ):
                    recommend.choose_speed("high", invalid)

    def test_invalid_latency_priority_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError, "latency_priority must be normal or high"
        ):
            recommend.choose_speed("urgent", True)

    def test_fast_candidate_records_explicit_preference_source(self):
        payload = PrepareTests().payload(60)
        payload["latency_priority"] = "high"
        payload["allow_fast"] = True

        prepared = recommend.prepare(
            payload, metrics([point("sol", "high", 93, 4, 20)]), insights(), NOW
        )

        self.assertEqual(prepared["candidates"][0]["speed"], "fast")
        self.assertEqual(prepared["speed_source"], "explicit_latency_preference")
        key = prepared["candidates"][0]["key"]

        fit = {
            key: {
                "effort": 0,
                "workload": 0,
                "latency": 4,
                "execution_horizon": 0,
                "reason": "latency priority",
            }
        }

        self.assertEqual(
            recommend.decide(prepared, fit)["speed_source"],
            "explicit_latency_preference",
        )

        self.assertTrue(recommend.decide(prepared, fit)["current_qualified"])

    def test_decide_derives_speed_source_from_controls(self):
        prepared = RankingTests().prepared()
        prepared["speed_source"] = "explicit_latency_preference"

        self.assertEqual(
            recommend.decide(prepared, RankingTests().fits())["speed_source"], "default"
        )
        del prepared["speed_source"]

        self.assertEqual(
            recommend.decide(prepared, RankingTests().fits())["speed_source"], "default"
        )
        prepared["candidates"] = []
        prepared["speed_source"] = "explicit_latency_preference"

        self.assertEqual(recommend.decide(prepared, {})["speed_source"], "default")
        payload = PrepareTests().payload(60)
        payload["latency_priority"] = "high"
        payload["allow_fast"] = True

        fast = recommend.prepare(
            payload, metrics([point("sol", "high", 93, 4, 20)]), insights(), NOW
        )
        fast["speed_source"] = "default"

        key = fast["candidates"][0]["key"]

        fit = {
            key: {
                "effort": 0,
                "workload": 0,
                "latency": 4,
                "execution_horizon": 0,
                "reason": "latency priority",
            }
        }

        self.assertEqual(
            recommend.decide(fast, fit)["speed_source"], "explicit_latency_preference"
        )


class RenderTests(unittest.TestCase):
    def test_render_returns_exact_strict_ambiguity_footer_without_radar(self):
        block = "下一次任务：补充具体系统、操作、影响范围与回滚方案\n推荐模型：无可验证推荐"

        self.assertEqual(
            recommend.run({"action": "render", "terminal_block": "strict_ambiguity"}),
            {"status": "rendered", "block": block},
        )

    def test_completed_work_has_no_legacy_unknown_task_renderer(self):
        with self.assertRaisesRegex(ValueError, "unknown terminal block"):
            recommend.run({"action": "render", "terminal_block": "next_task_unknown"})

    def test_render_rejects_unknown_terminal_block(self):
        with self.assertRaisesRegex(ValueError, "unknown terminal block"):
            recommend.run({"action": "render", "terminal_block": "other"})


class SkillContractTests(unittest.TestCase):
    def setUp(self):
        self.skill = (Path(__file__).parents[1] / "SKILL.md").read_text(
            encoding="utf-8"
        )

    def state(self, name):
        marker = f"### `{name}`"
        self.assertIn(marker, self.skill)
        section = self.skill.split(marker, 1)[1]
        next_heading = re.search("(?m)^#{2,3}\\s", section)
        if next_heading:
            return section[: next_heading.start()]

        return section

    def fenced_text(self, section):
        return section.split("```text\n", 1)[1].split("\n```", 1)[0]

    def test_terminal_machine_has_exactly_four_mutually_exclusive_states(self):
        self.assertIn("## Terminal state machine", self.skill)
        self.assertIn("## Unfinished-task footer", self.skill)

        machine = self.skill.split("## Terminal state machine", 1)[1].split(
            "## Unfinished-task footer", 1
        )[0]

        self.assertEqual(
            re.findall("(?m)^### `([A-Z_]+)`$", machine),
            [
                "PREFLIGHT_STOP_KNOWN",
                "PREFLIGHT_STOP_AMBIGUOUS",
                "HANDOFF_NEXT_DEFINED",
                "HANDOFF_NEXT_SUGGESTED",
            ],
        )

        self.assertIn("Choose exactly one terminal state", machine)
        self.assertNotIn("HANDOFF_NEXT_UNKNOWN", machine)
        self.assertNotIn("HANDOFF_COMPLETE", machine)

    def test_terminal_gate_distinguishes_unfinished_from_complete_work(self):
        self.assertIn("## Terminal-output gate", self.skill)

        gate = self.skill.split("## Terminal-output gate", 1)[1].split(
            "## Non-negotiable policy", 1
        )[0]

        for text in (
            "Before ending any turn",
            "PREFLIGHT_STOP_KNOWN",
            "PREFLIGHT_STOP_AMBIGUOUS",
            "HANDOFF_NEXT_DEFINED",
            "HANDOFF_NEXT_SUGGESTED",
            "exactly two lines",
            "proactively suggest",
        ):
            with self.subTest(text=text):
                self.assertIn(text, gate)

    def test_known_preflight_stop_ends_with_unblocking_task_and_model(self):
        state = self.state("PREFLIGHT_STOP_KNOWN")

        for text in (
            "before execution",
            "risk is knowable",
            "unblocking action",
            "unfinished-task footer",
        ):
            with self.subTest(text=text):
                self.assertIn(text, state)

    def test_ambiguous_preflight_stop_is_the_exact_two_line_footer(self):
        state = self.state("PREFLIGHT_STOP_AMBIGUOUS")
        self.assertIn("before execution", state)
        self.assertIn("strict mode", state)
        self.assertIn("two or more risk levels", state)

        self.assertEqual(
            self.fenced_text(state), "下一次任务：补充具体系统、操作、影响范围与回滚方案\n推荐模型：无可验证推荐"
        )

        self.assertIn("Never output mojibake", state)
        self.assertIn("Do not run `prepare` or `rank`", state)
        self.assertIn('"action": "render"', state)
        self.assertIn('"terminal_block": "strict_ambiguity"', state)
        self.assertIn("output its `block` field verbatim", state)

    def test_completed_work_suggests_one_grounded_adjacent_task_without_executing_it(
        self,
    ):
        state = self.state("HANDOFF_NEXT_SUGGESTED")

        for text in (
            "after the current work is complete",
            "overall task is complete",
            "proactively suggest exactly one",
            "grounded, adjacent, executable next task",
            "assess that suggested task",
            "This is an optional recommendation, not an inference about user intent.",
            "Prefix the task value with `建议：`",
            "require user confirmation",
            "exactly two-line footer",
            "Do not execute the suggested task",
            "Never output `下一任务未定义`",
            "Never recycle the completed current task",
            "multiple next tasks or a task list",
        ):
            with self.subTest(text=text):
                self.assertIn(text, state)

    def test_explicit_next_task_gets_exact_two_line_footer(self):
        state = self.state("HANDOFF_NEXT_DEFINED")
        self.assertIn("after the current work is complete", state)
        self.assertIn("user or plan explicitly defines a next task", state)
        self.assertIn("unfinished-task footer", state)
        self.assertIn("next task only", state)
        self.assertIn("Never recycle the completed current task", state)

    def test_unfinished_footer_has_exactly_task_and_model_lines(self):
        self.assertIn("## Unfinished-task footer", self.skill)

        block = self.skill.split("## Unfinished-task footer", 1)[1].split(
            "### Data insufficiency", 1
        )[0]

        self.assertEqual(
            self.fenced_text(block),
            "下一次任务：{明确的下一任务或解阻动作}\n推荐模型：{model · effort · Standard/Fast，或 无可验证推荐}",
        )

        self.assertIn("exactly two lines", block)

        for forbidden in ("风险：", "门槛：", "当前：", "动作：", "置信度：", "依据："):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.fenced_text(block))

    def test_terminal_labels_are_literal_unicode_not_mojibake(self):
        final = self.skill.split("## Unfinished-task footer", 1)[1]
        self.assertIn("Copy the literal Unicode labels", final)
        self.assertIn("Never output mojibake", final)

    def test_latest_explicit_preference_revokes_stale_fast_authorization(self):
        policy = self.skill.split("## Non-negotiable policy", 1)[1].split(
            "## Risk assessment", 1
        )[0]

        for text in (
            "Recommend `Fast` only when the current task, or the latest unoverridden explicit preference, prioritizes latency and accepts extra usage.",
            "A later `Standard` or cost-priority instruction revokes earlier `Fast` authorization.",
            "Task-local `Fast` authorization expires when that task ends.",
            "Only an explicitly persistent user preference may carry across tasks.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, policy)

    def test_luna_max_fast_preference_is_explicit_opt_in(self):
        policy = self.skill.split("## Non-negotiable policy", 1)[1].split(
            "## Risk assessment", 1
        )[0]
        workflow = self.skill.split("## Assessment workflow", 1)[1].split(
            "## Modes", 1
        )[0]
        policy_plain = policy.replace("`", "")
        workflow_plain = workflow.replace("`", "")

        for text in (
            "luna_max_fast_preference defaults to false",
            "only when the user explicitly enables it",
            "Luna is limited to max · Fast",
            "other models remain Standard by default",
            "otherwise pass false",
            "establishes it as an explicitly persistent user preference",
        ):
            with self.subTest(text=text):
                self.assertIn(text, policy_plain)

        for text in (
            "luna_max_fast_preference=true only if the user explicitly enables it",
            "establishes an explicitly persistent Luna-only max · Fast preference",
            "otherwise pass false",
            "does not automatically switch the current configuration",
            "does not make a quality-qualified current Luna non-max configuration fail the quality gate",
        ):
            with self.subTest(text=text):
                self.assertIn(text, workflow_plain)

    def test_luna_quality_baseline_is_explicit_opt_in_and_comparison_stays_standard(
        self,
    ):
        policy = self.skill.split("## Non-negotiable policy", 1)[1].split(
            "## Risk assessment", 1
        )[0]
        workflow = self.skill.split("## Assessment workflow", 1)[1].split(
            "## Modes", 1
        )[0]
        policy_plain = policy.replace("`", "")
        workflow_plain = workflow.replace("`", "")

        for text in (
            "luna_quality_baseline defaults to false",
            "only when the user explicitly enables it",
            "Luna max Standard IQ is the minimum quality floor",
            "Compare every candidate at Standard speed",
            "do not use Fast-mode speed to rank candidates",
            "Luna max Fast remains an output preference",
        ):
            with self.subTest(text=text):
                self.assertIn(text, policy_plain)

        for text in (
            "luna_quality_baseline=true only if the user explicitly enables it",
            "establishes an explicitly persistent quality-floor preference",
            "otherwise pass false",
            "Luna max quality baseline is unavailable",
            "comparison_speed=standard",
        ):
            with self.subTest(text=text):
                self.assertIn(text, workflow_plain)

    def test_release_skill_uses_relative_script_location(self):
        workflow = self.skill.split("## Assessment workflow", 1)[1].split(
            "## Modes", 1
        )[0]
        forbidden_user_path = "C:" + "\\Users\\silence"

        self.assertIn("scripts/recommend.py", workflow)
        self.assertNotIn(forbidden_user_path, self.skill)

    def test_qualified_switch_does_not_pause_solely_for_candidate_or_speed_difference(
        self,
    ):
        modes = self.skill.split("## Modes", 1)[1].split(
            "## Terminal state machine", 1
        )[0]

        self.assertIn(
            "Outside strict mode, a qualified task does not pause solely because the recommended candidate or speed differs from the current configuration.",
            modes,
        )

    def test_adaptive_change_notice_policy_is_documented(self):
        workflow = self.skill.split("## Assessment workflow", 1)[1].split(
            "## Modes", 1
        )[0]

        with self.subTest(
            text="derive task_horizon with the approved uncertain-duration default"
        ):
            self.assertIn(
                "Derive `task_horizon` before `prepare`; default to `short` when duration is uncertain.",
                workflow,
            )

        modes = self.skill.split("## Modes", 1)[1].split(
            "## Terminal state machine", 1
        )[0]

        for text in (
            "Short tasks continue without a notice when the current configuration is qualified.",
            "For a long task, emit one non-blocking `模型差距提醒（不中断）` only when a qualified recommendation lowers Radar average price by at least 50%.",
            "This notice never pauses a qualified task by itself and never changes the active configuration.",
            "关闭模型差距提醒",
            "Strict mode and an unknown or unqualified current configuration retain their existing pause protection.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, modes)

        no_notice_lines = [
            line for line in modes.splitlines() if "No notice is emitted" in line
        ]

        self.assertEqual(len(no_notice_lines), 1)

        if no_notice_lines:
            self.assertIn("an unqualified current configuration", no_notice_lines[0])

        for text in (
            "one concise normal-prose line headed `模型差距提醒（不中断）`",
            "关闭模型差距提醒 sets `notify_on_large_savings=false` for that task",
            "Do not claim token savings from `average_price_usd`.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, modes)

    def test_prepare_rederives_fast_controls_without_history_or_cache(self):
        workflow = self.skill.split("## Assessment workflow", 1)[1].split(
            "## Modes", 1
        )[0]

        for text in (
            "Before each `prepare`, re-derive `latency_priority` and `allow_fast` from the current task or active persistent preference.",
            "Never derive them from model history or Radar cache.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, workflow)

    def test_availability_completeness_policy_is_documented(self):
        workflow = self.skill.split("## Assessment workflow", 1)[1].split(
            "## Modes", 1
        )[0]

        policy = self.skill.split("## Non-negotiable policy", 1)[1].split(
            "## Risk assessment", 1
        )[0]

        self.assertIn(
            "Official OpenAI documentation may verify model existence and supported reasoning effort(s), but does not prove current account or Codex app availability.",
            policy,
        )

        for text in (
            "available_complete=true",
            "complete current Codex app model-picker list",
            "official documentation does not prove account availability",
            "available_complete=false",
            "Set `available_complete=false` for a manually supplied list.",
            "Set `available_complete=false` for a list inferred from history.",
            "When the complete app list is unknown, pass `available=null` while keeping `available_complete=false`; never pass `null` for the boolean marker.",
            "available model list is incomplete or unverified",
        ):
            with self.subTest(text=text):
                self.assertIn(text, workflow)

        self.assertNotIn(
            "remembered or partial list may be used as a compatibility allowlist",
            self.skill,
        )

    def test_force_l4_is_a_minimum_and_example_uses_actual_six_dimension_score(self):
        self.assertIn("### Force-L4 output", self.skill)

        policy = self.skill.split("### Force-L4 output", 1)[1].split(
            "### Data insufficiency", 1
        )[0]

        self.assertIn("minimum of 75", policy)
        self.assertIn("{max(实际六维分数, 75)}", policy)
        self.assertIn("actual six-dimension score", policy)
        self.assertIn("replace every placeholder", policy)
        self.assertNotIn("风险：L4，75/100", policy)

    def test_data_insufficiency_only_changes_recommendation_and_confidence(self):
        policy = self.skill.split("### Data insufficiency", 1)[1]
        self.assertIn("推荐模型：无可验证推荐", policy)
        self.assertIn("Do not copy the current configuration", policy)


class PriceRatioRegressionTests(unittest.TestCase):
    def test_zero_prices_are_deterministic(self):
        self.assertEqual(recommend.relative_price_scores([0.0, 0.0]), [100.0, 100.0])
        self.assertEqual(recommend.relative_price_scores([0.0, 0.5]), [100.0, 0.0])

    def test_equal_prices_remain_tied(self):
        self.assertEqual(recommend.relative_price_scores([]), [])
        self.assertEqual(recommend.relative_price_scores([2.0, 2.0]), [100.0, 100.0])

    def test_actual_price_ratio_uses_the_cheapest_candidate(self):
        self.assertEqual(recommend.relative_price_scores([0.5, 4.0]), [100.0, 12.5])

    def test_adding_a_more_expensive_candidate_does_not_shift_existing_scores(self):
        original = recommend.relative_price_scores([0.5, 4.0])
        extended = recommend.relative_price_scores([0.5, 4.0, 20.0])
        self.assertEqual(original, extended[:2])

    def test_luna_sol_ranking_uses_actual_price_ratio(self):
        case = RankingTests()
        prepared = case.prepared()
        ranked = {
            item["key"]: item
            for item in recommend.rank_candidates(prepared, case.fits(sol=0, luna=0))[
                "ranked"
            ]
        }
        self.assertAlmostEqual(ranked["sol|xhigh|standard"]["radar_score"], 33.3333331)
        self.assertAlmostEqual(ranked["luna|max|standard"]["radar_score"], 72.5)
        self.assertGreater(
            ranked["luna|max|standard"]["total_score"],
            ranked["sol|xhigh|standard"]["total_score"],
        )

    def test_invalid_relative_prices_are_rejected(self):
        for values in ({"price": 1.0}, [1.0, -1.0], [1.0, float("nan")], [True]):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "relative price"):
                    recommend.relative_price_scores(values)

    def test_skill_documents_actual_price_ratio(self):
        skill = (
            Path(__file__).parents[1].joinpath("SKILL.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            "Score price by actual average-price ratio to the cheapest qualified candidate, "
            "not by candidate-pool percentile rank.",
            skill,
        )


class RecoveredScopeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(cls.source, filename=__file__)
        cls.methods = {
            method.name: method
            for test_class in tree.body
            if isinstance(test_class, ast.ClassDef)
            for method in test_class.body
            if isinstance(method, ast.FunctionDef)
        }

    def test_boolean_validation_stays_inside_each_field_loop(self):
        method = self.methods["test_optional_boolean_payload_fields_are_validated"]
        field_loop = next(
            node
            for node in method.body
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Tuple)
            and [getattr(item, "id", None) for item in node.target.elts]
            == ["field", "message"]
        )
        field_source = ast.get_source_segment(self.source, field_loop) or ""

        self.assertIn('for value in (0, 1, "false"):', field_source)
        self.assertIn(
            'self.assertRaisesRegex(ValueError, f"^{message}$")', field_source
        )

    def test_each_incomplete_marker_keeps_public_and_rank_assertions(self):
        method = self.methods[
            "test_partial_available_list_cannot_hide_luna_or_enable_ranking"
        ]
        marker_loop = next(node for node in method.body if isinstance(node, ast.For))
        marker_source = ast.get_source_segment(self.source, marker_loop) or ""

        for expected in (
            'self.assertEqual(public["status"], "warn")',
            "ranked = recommend.run(",
            'self.assertEqual(ranked["status"], "pause")',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, marker_source)

    def test_cache_cleanup_assertions_stay_inside_temporary_directory(self):
        method = self.methods["test_live_data_is_cached_without_private_input"]
        temporary_directory = next(
            node for node in method.body if isinstance(node, ast.With)
        )
        temporary_source = (
            ast.get_source_segment(self.source, temporary_directory) or ""
        )

        for expected in (
            "self.assertEqual(len(staged), 2)",
            "self.assertNotEqual(*staged)",
            'self.assertEqual(list(Path(directory).glob("radar.json.*")), [])',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, temporary_source)


class ReadableTestSourceContractTests(unittest.TestCase):
    def test_active_test_source_has_no_bytecode_recovery_loader(self):
        source = Path(__file__).read_text(encoding="utf-8")
        artifact = "test_recommend" + ".recovery.pyc"
        loader = "marshal" + ".load"
        self.assertNotIn(artifact, source)
        self.assertNotIn(loader, source)


if __name__ == "__main__":
    unittest.main()
