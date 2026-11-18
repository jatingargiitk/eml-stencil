"""Every recorded example output must appear verbatim in the README."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = sorted((ROOT / "examples" / "expected").glob("*.txt"))


@pytest.mark.parametrize("path", EXPECTED, ids=lambda p: p.stem)
def test_example_is_in_readme(path):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8").rstrip("\n") in readme


def test_every_example_command_is_recorded():
    script = (ROOT / "examples" / "readme_examples.sh").read_text(encoding="utf-8")
    names = {line.split()[1] for line in script.splitlines() if line.startswith(("run ", "runs "))}
    names.add("serve")  # recorded by the block at the end of the script
    assert names == {p.stem for p in EXPECTED}
