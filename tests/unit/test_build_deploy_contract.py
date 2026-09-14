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
        deploy_line = next(
            line for line in script.splitlines() if 'tools\\deploy_runtime.py' in line
        )
        self.assertNotIn("2>&1", deploy_line)
        self.assertIn('1> "%DEPLOY_RECEIPT%"', deploy_line)
        self.assertIn('2> "%DEPLOY_DIAGNOSTIC%"', deploy_line)
        self.assertIn("WINDOWS_SUPPORTER_DEPLOY_RECEIPT=", script)
        self.assertLess(
            script.index('dist\\%EXE_NAME%" --google-calendar-resource-smoke'),
            script.index('tools\\deploy_runtime.py'),
        )

    def test_build_stages_tcltk_runtime_and_validates_frozen_init(self):
        script = (REPO_ROOT / "build.bat").read_text(encoding="utf-8")
        # Tcl/Tk script libraries must be staged as real _tcl_data/_tk_data
        # directories before PyInstaller runs, and must be shipped via
        # --add-data so the stock _tkinter run-time hook can export
        # TCL_LIBRARY/TK_LIBRARY inside the frozen process.
        self.assertIn("tools\\stage_tcltk_runtime.py", script)
        self.assertLess(
            script.index("tools\\stage_tcltk_runtime.py"),
            script.index("python -m PyInstaller"),
        )
        self.assertIn('tcltk\\_tcl_data;_tcl_data', script)
        self.assertIn('tcltk\\_tk_data;_tk_data', script)
        # The staged entry points must be verified inside the built archive so
        # a missing Tcl/Tk script library fails the build.
        self.assertIn('--entry "_tcl_data\\init.tcl"', script)
        self.assertIn('--entry "_tk_data\\tk.tcl"', script)
        # The frozen candidate must prove Tcl_Init/Tk_Init actually work before
        # the transactional deploy step is reached.
        self.assertLess(
            script.index('dist\\%EXE_NAME%" --tcl-runtime-smoke'),
            script.index('tools\\deploy_runtime.py'),
        )

    def test_runtime_deploy_cli_is_checked_in(self):
        self.assertTrue((REPO_ROOT / "tools" / "deploy_runtime.py").is_file())
        self.assertTrue((REPO_ROOT / "tools" / "stage_tcltk_runtime.py").is_file())


if __name__ == "__main__":
    unittest.main()
