from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path


sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    ".gitignore",
    "skill/SKILL.md",
    "skill/agents/openai.yaml",
    "skill/scripts/recommend.py",
    "skill/tests/test_recommend.py",
    "skill/tests/test_release_contract.py",
)
TEXT_SUFFIXES = {".md", ".py", ".txt", ".yaml", ".yml"}
FORBIDDEN_USER_PATH = "C:" + "\\Users\\silence"
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:api[_ -]?key|authorization|cookie|password|private[_ ]?key)"
    r"\s*[:=]\s*\S+"
)


class ReleaseContractTests(unittest.TestCase):
    def required_text(self, relative):
        path = ROOT / relative
        self.assertTrue(path.is_file(), f"missing required release file: {relative}")
        return path.read_text(encoding="utf-8")

    def test_required_release_layout_exists(self):
        for relative in REQUIRED_FILES:
            with self.subTest(relative=relative):
                self.assertTrue(
                    (ROOT / relative).is_file(),
                    f"missing required release file: {relative}",
                )

    def test_license_and_readme_make_only_public_claims(self):
        license_text = self.required_text("LICENSE")
        readme = self.required_text("README.md")

        self.assertIn(
            "Permission is hereby granted, free of charge, to any person obtaining a copy",
            license_text,
        )
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', license_text)
        for phrase in (
            "CodexRadar",
            "Python 3.9+",
            "does not automatically switch",
            "not affiliated with, endorsed by, or supported by OpenAI or CodexRadar.",
            "[MIT](LICENSE)",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)

    def test_readme_distinguishes_verified_and_radar_only_recommendations(self):
        readme = self.required_text("README.md")

        for phrase in (
            "account-verified configuration",
            "Radar-only",
            "account availability is unverified",
            "does not read, click, scrape, or infer the native model picker",
            "known Codex model family",
            "third-party Radar model",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)

    def test_release_has_no_generated_artifacts_or_personal_text(self):
        artifacts = []
        for path in ROOT.rglob("*"):
            if (
                path.name
                in {"__pycache__", ".pytest_cache", "test_recommend.recovery.pyc"}
                or path.suffix.lower() in {".pyc", ".pyo"}
            ):
                artifacts.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(artifacts, [], f"generated artifacts found: {artifacts}")

        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertNotIn(FORBIDDEN_USER_PATH, text)
                self.assertNotRegex(text, SENSITIVE_ASSIGNMENT)

    def test_skill_is_relative_and_luna_options_are_opt_in(self):
        skill = self.required_text("skill/SKILL.md")

        self.assertIn("scripts/recommend.py", skill)
        self.assertNotIn(FORBIDDEN_USER_PATH, skill)
        self.assertRegex(
            skill,
            r"(?m)^- `luna_max_fast_preference` defaults to `false`\.",
        )
        self.assertRegex(
            skill,
            r"(?m)^- `luna_quality_baseline` defaults to `false`\.",
        )
        self.assertIn("only when the user explicitly enables it", skill)
        self.assertNotIn("this user's persistent", skill)

    def test_runtime_defaults_are_general_not_personal(self):
        script = ROOT / "skill" / "scripts" / "recommend.py"
        self.assertTrue(script.is_file(), "release runtime is missing")

        spec = importlib.util.spec_from_file_location("release_recommend", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        controls = module._payload_controls({})

        self.assertEqual(controls[:5], (False, False, False, False, False))
        self.assertEqual(controls[5:8], ("normal", "standard", "short"))
        self.assertTrue(controls[8])


if __name__ == "__main__":
    unittest.main()
