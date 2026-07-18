from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class ReleaseChainGateContractTest(unittest.TestCase):
    def test_release_chain_sets_korea_timezone_before_running_tests(self) -> None:
        release_workflow = (REPO_ROOT / ".github/workflows/release-chain-gate.yml").read_text(
            encoding="utf-8"
        )

        expected_sequence = (
            '      - name: Configure target timezone\n'
            '        shell: cmd\n'
            '        run: |\n'
            '          tzutil /s "Korea Standard Time"\n'
            '          if errorlevel 1 exit /b 1\n'
            '          tzutil /g | findstr /x /c:"Korea Standard Time"\n'
            '          if errorlevel 1 exit /b 1\n'
            '      - name: Run release tests\n'
        )
        self.assertIn(expected_sequence, release_workflow)

    def test_release_chain_artifact_name_is_safe_for_slash_branches(self) -> None:
        release_workflow = (REPO_ROOT / ".github/workflows/release-chain-gate.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'name: windows-supporter-${{ github.run_id }}-${{ github.sha }}',
            release_workflow,
        )
        self.assertNotIn(
            'name: windows-supporter-${{ github.ref_name }}-${{ github.sha }}',
            release_workflow,
        )


if __name__ == "__main__":
    unittest.main()
