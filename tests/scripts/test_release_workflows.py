"""Offline regressions for the actual inline release workflow scripts."""

import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/release.yml"
REPO = "fireflyframework/fireflyframework-pyfly"
TAG = "a" * 40
TREE = "b" * 40
HEAD = "c" * 40
MERGE = "d" * 40


def script(name):
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "- name: " + name)
    run = next(i for i in range(start + 1, len(lines)) if lines[i] == "        run: |")
    end = next(
        (i for i in range(run + 1, len(lines)) if lines[i].strip() and not lines[i].startswith("          ")),
        len(lines),
    )
    return textwrap.dedent("\n".join(lines[run + 1 : end]))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        shell = script("Require trusted CI evidence and main ancestry")
        self.code = shell.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        self.run_data = dict(
            id=123,
            run_attempt=1,
            conclusion="success",
            status="completed",
            path=".github/workflows/ci.yml",
            repository={"full_name": REPO},
            head_repository={"full_name": REPO},
            event="pull_request",
            head_branch="feature",
            head_sha=HEAD,
            html_url="https://github.com/" + REPO + "/actions/runs/123",
        )
        self.evidence = dict(repository=REPO, run_id="123", run_attempt="1", commit=MERGE, tree=TREE)
        self.commit = dict(tree={"sha": TREE}, parents=[{"sha": HEAD}])
        self.ancestor = True
        self.available = True
        self.queries = []

    def check_output(self, args, **kwargs):
        self.queries.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return (TREE if args[2] == "HEAD^{tree}" else TAG) + "\n"
        if args[:2] == ["gh", "api"]:
            if "/actions/workflows/ci.yml/runs?" in args[2]:
                return json.dumps({"workflow_runs": [self.run_data]})
            if "/git/commits/" in args[2]:
                return json.dumps(self.commit)
        raise AssertionError(args)

    def command(self, args, **kwargs):
        if args[:2] == ["git", "merge-base"]:
            if not self.ancestor:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["gh", "run", "download"]:
            if self.available:
                (Path(args[args.index("--dir") + 1]) / "ci-verification.json").write_text(json.dumps(self.evidence))
            return subprocess.CompletedProcess(args, 0 if self.available else 1)
        raise AssertionError(args)

    def execute(self):
        original_temp = tempfile.TemporaryDirectory
        output = io.StringIO()

        def local_temp():
            return original_temp(prefix="pyfly-release-test-")

        with (
            patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO}),
            patch("subprocess.check_output", self.check_output),
            patch("subprocess.run", self.command),
            patch("tempfile.TemporaryDirectory", local_temp),
            redirect_stdout(output),
        ):
            exec(compile(self.code, str(WORKFLOW), "exec"), {})
        return output.getvalue()

    def test_pr_merge_tree_matches_squash_tag_with_different_sha(self):
        self.assertEqual(
            self.execute(),
            f"Verified {TAG} (tree {TREE}) against https://github.com/{REPO}/actions/runs/123\n",
        )

    def test_exact_main_commit(self):
        self.run_data.update(event="push", head_branch="main", head_sha=TAG)
        self.evidence["commit"] = TAG
        self.assertEqual(
            self.execute(),
            f"Verified {TAG} (tree {TREE}) against https://github.com/{REPO}/actions/runs/123\n",
        )

    def test_changed_descendant_is_rejected(self):
        self.evidence["tree"] = "e" * 40
        with self.assertRaises(SystemExit):
            self.execute()

    def test_forged_tree_is_rejected_against_git_object(self):
        self.commit["tree"]["sha"] = "e" * 40
        with self.assertRaises(SystemExit):
            self.execute()

    def test_fork_is_rejected_without_downloading(self):
        self.run_data["head_repository"]["full_name"] = "attacker/fork"
        with self.assertRaises(SystemExit):
            self.execute()
        self.assertEqual(len(self.queries), 3)

    def test_wrong_workflow_is_rejected(self):
        self.run_data["path"] = ".github/workflows/fake.yml"
        with self.assertRaises(SystemExit):
            self.execute()

    def test_failed_ci_is_rejected(self):
        self.run_data["conclusion"] = "failure"
        with self.assertRaises(SystemExit):
            self.execute()

    def test_missing_artifact_is_rejected(self):
        self.available = False
        with self.assertRaises(SystemExit):
            self.execute()

    def test_wrong_attempt_is_rejected(self):
        self.evidence["run_attempt"] = "0"
        with self.assertRaises(SystemExit):
            self.execute()

    def test_malicious_commit_is_rejected(self):
        self.evidence["commit"] = "../../attacker"
        with self.assertRaises(SystemExit):
            self.execute()

    def test_pr_head_not_in_tested_merge_is_rejected(self):
        self.commit["parents"] = [{"sha": "e" * 40}]
        with self.assertRaises(SystemExit):
            self.execute()

    def test_tag_outside_main_is_rejected(self):
        self.ancestor = False
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()


FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ['MOCK_STATE'])
data = json.loads(state.read_text())
args = sys.argv[1:]
with Path(os.environ['MOCK_LOG']).open('a') as log:
    log.write(json.dumps(args) + '\\n')
verb = args[1]
if verb == 'view':
    if not data['exists']:
        sys.exit(1)
    print(json.dumps({'isDraft': data['draft']}))
elif verb == 'create':
    if data['exists']:
        sys.exit(1)
    data['exists'] = True
    data['draft'] = '--draft' in args
    state.write_text(json.dumps(data))
    if data.get('race'):
        sys.exit(1)
elif verb in {'edit', 'upload'}:
    if not data['exists'] or not data['draft']:
        sys.exit(9)
    if verb == 'edit' and '--draft' not in args:
        sys.exit(10)
else:
    sys.exit(11)
"""


class DraftTests(unittest.TestCase):
    def check_case(self, exists, draft=True, race=False, expected=0):
        code = script("Update or create the draft release with assets")
        with tempfile.TemporaryDirectory(prefix="pyfly-release-test-") as directory:
            root = Path(directory)
            gh = root / "gh"
            gh.write_text(FAKE_GH)
            gh.chmod(0o700)
            (root / "state.json").write_text(json.dumps(dict(exists=exists, draft=draft, race=race)))
            env = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                MOCK_STATE=str(root / "state.json"),
                MOCK_LOG=str(root / "log.jsonl"),
                RUNNER_TEMP=str(root),
                TAG_NAME="v26.11.08",
                REPO=REPO,
                USE_NOTES_FILE="1",
            )
            result = subprocess.run(["bash", "-c", code], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            calls = [json.loads(line) for line in (root / "log.jsonl").read_text().splitlines()]
            if expected == 0:
                self.assertTrue(any(call[1] == "upload" and "--clobber" in call for call in calls))
                self.assertTrue(json.loads((root / "state.json").read_text())["draft"])
            else:
                self.assertFalse(any(call[1] in {"edit", "upload"} for call in calls))
            return calls

    def test_existing_draft_updates_without_create(self):
        calls = self.check_case(True)
        self.assertFalse(any(call[1] == "create" for call in calls))

    def test_missing_release_is_created_as_draft(self):
        calls = self.check_case(False)
        self.assertTrue(any(call[1] == "create" and "--draft" in call and "--verify-tag" in call for call in calls))

    def test_concurrent_draft_creation_recovers(self):
        self.check_case(False, race=True)

    def test_published_release_is_not_modified(self):
        self.check_case(True, draft=False, expected=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
