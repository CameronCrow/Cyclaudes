# TESTS

## Running them

```bash
pip install -e ".[dev]"   # required: the abstention plugin loads via entry point
python -m pytest          # whole suite
python -m pytest -v       # see each outcome by name (PASSED / FAILED / ABSTAINED)
python -m pytest tests/test_abstain_outcome.py   # just the trust boundary
```

## Layout

| File | Covers |
|---|---|
| `conftest.py` | Shared wiring. Currently only enables `pytester`. Discipline-layer fixtures land here separately (issue #3). |
| `test_abstain.py` | The abstention vocabulary — `CannotVerify`, `cannot_verify()`, `abstain_on()`. Plain unit tests. |
| `test_abstain_outcome.py` | The pytest integration. Runs pytest inside pytest via `pytester` so it asserts on real outcomes, real summary text and real exit codes. |

## Three outcomes, not two

A Cyclaudes check has three possible results. The suite's job is proving they
stay distinct — especially that the third can never be read as the first.

| Result | Meaning | Letter | Exit code |
|---|---|---|---|
| pass | verified: the expected UI state was observed | `.` | `0` |
| fail | falsified: the UI is in a state it should not be | `F` | `1` |
| **abstain** | **could not be evaluated — nothing was confirmed** | `A` | `12` |

Abstain is the load-bearing one. A false-positive "verified" is worse than the
manual stall Cyclaudes replaces, so `test_abstain_outcome.py` asserts the
abstention is unmistakable on every surface an agent might read: the progress
letter, the verbose word, the counts line, the dedicated `CANNOT VERIFY`
section, the process exit code, the JUnit XML, and the raw `report.outcome`.

## Writing a check that abstains

```python
from cyclaudes import CannotVerify, abstain_on, cannot_verify

def test_save_button_is_disabled_on_an_empty_form(window):
    if "Save" not in window.names():
        cannot_verify("Save button absent from the tree; nothing to assert on")
    assert window.state("Save") == "disabled"

def test_reads_the_tree(backend):
    with abstain_on(PermissionError, reason="no accessibility permission"):
        tree = backend.snapshot()
    assert tree
```

Abstain freely — it is a normal path, not an error path. The only wrong move is
guessing.
