# Internal Routing: Workspace Git Profile

Public contract: `../SKILL.md`

Use this file only after the git-profile contract has been selected.

## Command Mapping

- primary: `tools/vaws.py git-profile`

## Internal Behavior Notes

- populate repository topology and auth refs for personalized workspace use
- keep workspace and submodule origin configuration consistent with the staged lifecycle
- do not treat this as a server attachment or readiness probe step

## Internal State Touched

- `.workspace.local/repos.yaml`
- `.workspace.local/auth.yaml`

## Related Tests

- `tests/test_vaws_git_profile.py`
