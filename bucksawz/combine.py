"""
`bucksawz combine-stacks`: collect `tofu show -json` from every Terramate
stack under a repo and merge them into one `{stack_path: <show json>, ...}`
document -- the "multi_stack_show" shape `tf_state.detect_format` recognises,
so it works directly as a `bucksawz forecast` `actual`/`proposed` command.

Two modes, matching those two roles:

    state  ->  `tofu show -json`                        what is deployed now
    plan   ->  `tofu plan -out=F` + `tofu show -json F` what a full apply would give

Only these read-only tofu invocations are ever run (never apply/destroy/
import), as an argv list with no shell. `plan` refreshes against AWS
(read-only API calls) and passes `-lock=false` so it doesn't take the remote
state lock. Each stack must already be `tofu init`ed.

Stacks are listed by `terramate list` (run in the repo root), optionally
filtered by tags and restricted to directory prefixes such as
`stacks/management` or `stacks/workloads/dev`. Keys in the output are the
stacks' paths relative to the repo root, so `actual` and `proposed` runs line
up project by project.

Each stack's own AWS account needs its own credentials -- there's no
assume_role in play (see terramate.tm.hcl), just per-account aws-vault
profiles run by hand today. `--account-profile PREFIX=PROFILE` (repeatable)
supplies that mapping: the longest matching directory prefix for a stack
picks the profile, and its tofu invocation is wrapped in
`aws-vault exec PROFILE -- ...`. A stack matching no prefix runs with
whatever credentials are already ambient -- fine for `state`, since the
state bucket's policy grants any org-account principal read/write on
state/* (see stacks/management/main.tf's OrgReadWriteState statement), but
`plan` also configures the AWS provider and refreshes against the target
account, so an unmapped stack's plan will run against the wrong account's
API and should be expected to fail or mislead.
"""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

MODES = ("state", "plan")


class CombineError(RuntimeError):
    pass


def _run(argv: list[str], cwd: Optional[str] = None) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").strip().splitlines()[-15:])
        raise CombineError(f"command failed ({result.returncode}): {' '.join(argv)}\n{tail}")
    return result.stdout


def _under(stack: str, prefixes: list[str]) -> bool:
    return not prefixes or any(stack == p or stack.startswith(p + "/") for p in prefixes)


def parse_account_profiles(pairs: list[str]) -> dict[str, str]:
    """`["stacks/workloads/dev=dev", ...]` -> {"stacks/workloads/dev": "dev"}."""
    out = {}
    for pair in pairs:
        prefix, sep, profile = pair.partition("=")
        if not sep or not prefix or not profile:
            raise CombineError(f"--account-profile must be PREFIX=PROFILE, got {pair!r}")
        out[prefix.strip("/")] = profile
    return out


def _profile_for(stack: str, account_profiles: Optional[dict[str, str]]) -> Optional[str]:
    """Longest matching directory prefix wins, so a narrower override (e.g.
    stacks/workloads/dev/eu-west-2) can differ from its parent's."""
    if not account_profiles:
        return None
    match = max(
        (p for p in account_profiles if stack == p or stack.startswith(p + "/")),
        key=len, default=None,
    )
    return account_profiles.get(match) if match else None


def list_stacks(
    root: str, dirs: Optional[list[str]] = None, tags: Optional[str] = None, no_tags: Optional[str] = None,
    terramate_bin: str = "terramate",
) -> list[str]:
    argv = [terramate_bin, "list"]
    if tags:
        argv.append(f"--tags={tags}")
    if no_tags:
        argv.append(f"--no-tags={no_tags}")
    prefixes = [d.strip("/") for d in (dirs or [])]
    stacks = [line.strip() for line in _run(argv, cwd=root).splitlines() if line.strip()]
    return [s for s in stacks if _under(s, prefixes)]


def _wrap(argv: list[str], profile: Optional[str], aws_vault_bin: str) -> list[str]:
    return [aws_vault_bin, "exec", profile, "--", *argv] if profile else argv


def collect_stack(
    root: str, stack: str, mode: str, tofu_bin: str = "tofu",
    profile: Optional[str] = None, aws_vault_bin: str = "aws-vault",
) -> dict:
    """`tofu show -json` for one stack (its state, or a fresh plan). `profile`
    (an aws-vault profile name) runs the tofu invocation under that
    account's credentials -- see the module docstring."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    chdir = f"-chdir={Path(root) / stack}"
    if mode == "state":
        text = _run(_wrap([tofu_bin, chdir, "show", "-json", "-no-color"], profile, aws_vault_bin))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = str(Path(tmp) / "stack.plan")
            _run(_wrap(
                [tofu_bin, chdir, "plan", "-input=false", "-lock=false", "-no-color", f"-out={plan_file}"],
                profile, aws_vault_bin,
            ))
            text = _run(_wrap([tofu_bin, chdir, "show", "-json", "-no-color", plan_file], profile, aws_vault_bin))
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise CombineError(f"{stack}: tofu show did not produce JSON: {e}") from e


def combine_stacks(
    root: str, mode: str, dirs: Optional[list[str]] = None, tags: Optional[str] = None,
    no_tags: Optional[str] = None, parallel: int = 4, skip_failed: bool = False,
    tofu_bin: str = "tofu", terramate_bin: str = "terramate",
    account_profiles: Optional[dict[str, str]] = None, aws_vault_bin: str = "aws-vault",
) -> dict[str, dict]:
    stacks = list_stacks(root, dirs, tags, no_tags, terramate_bin)
    if not stacks:
        raise CombineError("no stacks matched (check --dir / --tags / --no-tags)")

    def one(stack: str):
        profile = _profile_for(stack, account_profiles)
        print(f"[{mode}] {stack}" + (f" (aws-vault: {profile})" if profile else ""), file=sys.stderr)
        try:
            return stack, collect_stack(root, stack, mode, tofu_bin, profile, aws_vault_bin), None
        except CombineError as e:
            return stack, None, str(e)

    combined: dict[str, dict] = {}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        for stack, data, err in pool.map(one, stacks):
            if err is None:
                combined[stack] = data
            else:
                failures.append(err)
    if failures and not skip_failed:
        raise CombineError(f"{len(failures)} of {len(stacks)} stacks failed:\n" + "\n".join(failures))
    for err in failures:
        print(f"skipped: {err}", file=sys.stderr)
    return dict(sorted(combined.items()))
