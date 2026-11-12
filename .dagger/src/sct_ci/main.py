"""Spike E7 (SCT-1491): SCT PR checks, the hydra image and one loader image as Dagger functions.

Examples, from the repository root:
    dagger call hydra-tag
    dagger call hydra export-image --name scylladb/hydra:spike
    dagger call hydra-oci export --path /tmp/hydra-multiarch.tar
    dagger call check --name unit-tests
    dagger call checks
    dagger call ycsb --branch master with-exec --args ls,/YCSB/bin stdout
"""

import asyncio
import hashlib
from typing import Annotated

import dagger
from dagger import DefaultPath, Doc, Ignore, dag, function, object_type

# The files the root Dockerfile reads. The tag hash, and the build context, see only these.
HYDRA_INPUTS = ("Dockerfile", "pyproject.toml", "uv.lock")

# The PR checks of the root Jenkinsfile, with the commands its stages pass to hydra.sh.
CHECKS = {
    # pre-commit needs a git repository. A worktree's .git points outside the tree, so a fresh one is made and
    # committed: check-added-large-files flags every staged new file (uv.lock is 874 KB).
    # ponytail: no commit-message lint (scripts/lint_commits.sh needs the PR history); pass a full clone to add it.
    "pre-commit": (
        "git init -q && git add -A && git -c user.name=ci -c user.email=ci@localhost commit -q --no-verify -m tree"
        " && pre-commit run -a --show-diff-on-failure"
    ),
    "unit-tests": "python sct.py unit-tests -n 4",
    "collect": "python -m pytest --collect-only -q -p no:cacheprovider *_test.py",
    "lint-pipelines": "python sct.py lint-pipelines",
}

Tree = Annotated[
    dagger.Directory,
    DefaultPath("/"),
    Ignore([".venv", ".git", "**/__pycache__", ".ruff_cache", ".pytest_cache", "latest", "sct_runner_ip"]),
    Doc("SCT source tree"),
]


def hydra_tag_of(contents: dict[str, str]) -> str:
    """`h-` + the first 12 hex digits of `sha256sum Dockerfile pyproject.toml uv.lock | sha256sum`.

    The same value comes from bash, so plain buildx and Dagger agree on the tag.
    """
    lines = "".join(f"{hashlib.sha256(contents[name].encode()).hexdigest()}  {name}\n" for name in HYDRA_INPUTS)
    return "h-" + hashlib.sha256(lines.encode()).hexdigest()[:12]


@object_type
class SctCi:
    @function
    async def hydra_tag(self, src: Tree) -> str:
        """Content tag of the hydra image: changes only when its inputs change."""
        contents = await asyncio.gather(*(src.file(name).contents() for name in HYDRA_INPUTS))
        return hydra_tag_of(dict(zip(HYDRA_INPUTS, contents)))

    @function
    def hydra(self, src: Tree, platform: dagger.Platform | None = None) -> dagger.Container:
        """The hydra image built from the tree."""
        return src.filter(include=list(HYDRA_INPUTS)).docker_build(platform=platform)

    @function
    def hydra_oci(self, src: Tree, platforms: list[str] | None = None) -> dagger.File:
        """A multi-platform OCI tarball of the hydra image (default: amd64 and arm64)."""
        variants = [self.hydra(src, dagger.Platform(p)) for p in platforms or ["linux/amd64", "linux/arm64"]]
        return dag.container().as_tarball(platform_variants=variants)

    def _check(self, src: Tree, name: str) -> dagger.Container:
        return (
            self.hydra(src)
            .with_mounted_cache("/root/.cache/pre-commit", dag.cache_volume("sct-pre-commit"))
            .with_directory("/sct", src)
            .with_workdir("/sct")
            .with_env_variable("_SCT_BASE_DIR", "/sct")
            .with_exec(["bash", "-c", CHECKS[name]], expect=dagger.ReturnType.ANY)
        )

    @function
    async def check(self, src: Tree, name: Annotated[str, Doc(f"one of {', '.join(CHECKS)}")]) -> str:
        """Run one PR check; fail with its output when it fails."""
        ctr = self._check(src, name)
        out = await ctr.combined_output()
        if code := await ctr.exit_code():
            raise RuntimeError(f"{name} failed with exit code {code}:\n{out[-5000:]}")
        return out

    @function
    async def checks(self, src: Tree) -> str:
        """Run all PR checks in parallel in one engine; report each, fail if any fails."""
        ctrs = {name: self._check(src, name) for name in CHECKS}
        codes = await asyncio.gather(*(ctr.exit_code() for ctr in ctrs.values()))
        report = "\n".join(f"{'PASS' if code == 0 else 'FAIL'} {name} (exit {code})" for name, code in zip(ctrs, codes))
        if any(codes):
            raise RuntimeError(report)
        return report

    @function
    def ycsb(
        self,
        src: Annotated[dagger.Directory, DefaultPath("/docker/ycsb")],
        repo: str = "https://github.com/scylladb/YCSB.git",
        branch: str = "master",
    ) -> dagger.Container:
        """The YCSB loader image from docker/ycsb, built from any fork and branch."""
        return src.docker_build(
            build_args=[dagger.BuildArg(name="REPO", value=repo), dagger.BuildArg(name="BRANCH", value=branch)]
        )
