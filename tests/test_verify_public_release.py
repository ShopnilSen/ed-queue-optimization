from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_public_release.py"
SPEC = importlib.util.spec_from_file_location("verify_public_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verify_public_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_public_release)

REQUIRED = {
    "LICENSE": "MIT License\n",
    "NOTICE": "Public release notice\n",
    "README.md": "# Public release\n",
    ".env.example": "OPENAI_API_KEY=\n",
    "pyproject.toml": "[project]\nname = \"example\"\n",
    "scripts/verify_public_release.py": "# check\n",
}


class VerifyPublicReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for relative, content in REQUIRED.items():
            self.write(relative, content)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, content: str = "") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_clean_tree_passes(self) -> None:
        errors, stats = verify_public_release.check_tree(self.root)
        self.assertEqual(errors, [])
        self.assertEqual(stats["files"], len(REQUIRED))

    def test_csv_is_rejected(self) -> None:
        self.write("table.csv", "a,b\n1,2\n")
        errors, _ = verify_public_release.check_tree(self.root)
        self.assertTrue(any("forbidden file type" in error for error in errors))

    def test_data_directory_is_rejected(self) -> None:
        self.write("data/raw/notes.txt", "private\n")
        errors, _ = verify_public_release.check_tree(self.root)
        self.assertTrue(any("forbidden top-level directory" in error for error in errors))

    def test_research_directory_is_rejected(self) -> None:
        self.write("research/project/brief.md", "private\n")
        errors, _ = verify_public_release.check_tree(self.root)
        self.assertTrue(any("forbidden top-level directory" in error for error in errors))

    def test_env_file_is_rejected(self) -> None:
        self.write(".env", "OPENAI_API_KEY=\n")
        errors, _ = verify_public_release.check_tree(self.root)
        self.assertTrue(any("environment file" in error for error in errors))

    def test_pdf_is_rejected(self) -> None:
        self.write("paper.pdf", "%PDF-1.4\n")
        errors, _ = verify_public_release.check_tree(self.root)
        self.assertTrue(any("forbidden file type" in error for error in errors))

    def test_license_is_allowed(self) -> None:
        self.write("LICENSE", "MIT License\n\nPermission is hereby granted.\n")
        errors, _ = verify_public_release.check_tree(self.root)
        self.assertFalse(any("license" in error.lower() for error in errors))

    def test_credential_is_rejected_without_disclosure(self) -> None:
        value = "sk-" + ("A" * 40)
        self.write("notes.py", f"VALUE = '{value}'\n")
        errors, _ = verify_public_release.check_tree(self.root)
        self.assertTrue(any("possible credential pattern" in error for error in errors))
        self.assertFalse(any(value in error for error in errors))

    def test_private_path_is_rejected(self) -> None:
        self.write("notes.md", "Local path: C:\\triage\\research\n")
        errors, _ = verify_public_release.check_tree(self.root)
        self.assertTrue(any("private workspace path" in error for error in errors))

    def test_missing_required_file_is_rejected(self) -> None:
        (self.root / "LICENSE").unlink()
        errors, _ = verify_public_release.check_tree(self.root)
        self.assertTrue(any("required release file is missing" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
