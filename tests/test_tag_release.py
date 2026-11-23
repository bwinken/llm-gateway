"""``scripts/tag_release.sh``: CI tags a merge to main with its version.

The version is written in ``pyproject.toml`` and raised by hand in every
pull request. The script only tags: it never changes the version, never
moves a tag, and a merge that did not raise the version gets no tag. Run
here for real, against a throwaway repository and a bare remote."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "tag_release.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class Repo:
    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "work"
        self.remote = tmp_path / "remote.git"
        # No author configured anywhere, as on a CI runner.
        self.env = {
            **os.environ, "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.path.mkdir()
        self.git("init", "-q", "--bare", str(self.remote), cwd=tmp_path)
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", str(self.remote))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.path, env=self.env, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, version: str) -> str:
        (self.path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
        self.git("add", "-A")
        self.git("-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", f"version {version!r}")
        return self.git("rev-parse", "HEAD")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), *args], cwd=self.path, env=self.env, capture_output=True, text=True,
        )

    def remote_tags(self) -> dict[str, str]:
        """Tag name → the commit it points at, as the remote has them."""
        out = self.git("ls-remote", "--tags", "origin")
        tags: dict[str, str] = {}
        for line in out.splitlines():
            sha, ref = line.split("\t")
            name = ref.removeprefix("refs/tags/")
            if name.endswith("^{}"):
                tags[name[:-3]] = sha            # an annotated tag, peeled to its commit
            else:
                tags.setdefault(name, sha)
        return tags


@pytest.fixture()
def repo(tmp_path) -> Repo:
    return Repo(tmp_path)


class TestTagging:
    def test_a_merge_is_tagged_with_its_version(self, repo):
        commit = repo.commit("0.3.0")
        result = repo.run()
        assert result.returncode == 0, result.stderr
        assert repo.remote_tags() == {"v0.3.0": commit}
        assert "Tagged" in result.stdout
        # Annotated, so it carries who and when.
        assert repo.git("cat-file", "-t", "v0.3.0") == "tag"

    def test_run_again_on_the_same_commit_does_nothing(self, repo):
        commit = repo.commit("0.3.0")
        repo.run()
        again = repo.run()
        assert again.returncode == 0 and "already this commit" in again.stdout
        assert repo.remote_tags() == {"v0.3.0": commit}

    def test_a_merge_that_did_not_raise_the_version_gets_no_tag_and_moves_none(self, repo):
        first = repo.commit("0.3.0")
        repo.run()
        repo.commit("0.3.0")
        result = repo.run()
        assert result.returncode == 0
        assert "::warning::" in result.stdout and "did not raise the version" in result.stdout
        assert repo.remote_tags() == {"v0.3.0": first}

    def test_the_next_version_gets_its_own_tag(self, repo):
        first = repo.commit("0.3.0")
        repo.run()
        second = repo.commit("0.3.1")
        assert repo.run().returncode == 0
        assert repo.remote_tags() == {"v0.3.0": first, "v0.3.1": second}

    def test_a_dry_run_changes_nothing(self, repo):
        repo.commit("0.3.0")
        result = repo.run("--dry-run")
        assert result.returncode == 0 and "Would tag" in result.stdout
        assert repo.remote_tags() == {}

    @pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-rc1", "1.0.0; rm -rf x", ""])
    def test_a_version_that_is_not_three_numbers_is_refused(self, repo, version):
        repo.commit(version)
        result = repo.run()
        assert result.returncode == 1 and "::error::" in result.stderr
        assert repo.remote_tags() == {}

    def test_it_never_changes_the_version(self, repo):
        repo.commit("0.3.0")
        before = (repo.path / "pyproject.toml").read_text()
        repo.run()
        assert (repo.path / "pyproject.toml").read_text() == before
        assert repo.git("status", "--porcelain") == ""


class TestTheJob:
    """How CI runs it (``.github/workflows/ci.yml``)."""

    def _job(self) -> str:
        text = WORKFLOW.read_text(encoding="utf-8")
        return text[text.index("\n  tag:\n"):]

    def test_only_a_merge_to_main_that_passed_the_tests_is_tagged(self):
        job = self._job()
        assert "needs: test" in job
        assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in job
        assert "bash scripts/tag_release.sh" in job

    def test_only_that_job_may_write(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        assert text.count("contents: write") == 1 and "contents: write" in self._job()
        assert re.search(r"^permissions:\n  contents: read$", text, re.M)

    def test_a_push_to_main_is_never_superseded(self):
        """Two merges minutes apart must both be tested and tagged: only a
        pull request's runs share a group and cancel one another."""
        text = WORKFLOW.read_text(encoding="utf-8")
        assert "group: ci-${{ github.event_name == 'pull_request' && github.ref || github.sha }}" in text
