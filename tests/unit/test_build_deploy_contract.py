from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


class BuildDeployContractTest(unittest.TestCase):
    def test_build_creates_candidate_before_transactional_deploy(self):
        script = (REPO_ROOT / "build.bat").read_text(encoding="utf-8")
        self.assertNotIn("WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN", script)
        self.assertNotIn('taskkill /f /t /im "%EXE_NAME%"', script)
        self.assertIn('tools\\deploy_runtime.py', script)
        self.assertIn('WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY', script)
        self.assertLess(
            script.index('dist\\%EXE_NAME%" --google-calendar-resource-smoke'),
            script.index('tools\\deploy_runtime.py'),
        )

    def test_runtime_deploy_cli_is_checked_in(self):
        self.assertTrue((REPO_ROOT / "tools" / "deploy_runtime.py").is_file())


if __name__ == "__main__":
    unittest.main()
