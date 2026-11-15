# Spike E7: Dagger where containers are the product

**Jira:** SCT-1491 (epic SCT-1490) · **Date:** 2026-11-13 · **Branch:** `am/e7-dagger-spike` on the fork

> Experiment 7 of `spec.md`. Hypothesis: Dagger (Python SDK) adds value on top of option A for the PR checks,
> the hydra image, the helper images, and agent sandboxes. The spike ran on a laptop (16 cores, 61 GB RAM, a disk
> 93% full) and on GitHub Actions on the fork. No Jenkins job ran. No release branch changed.

## Summary

Dagger works for every item in scope, but it gives SCT nothing that plain buildx and plain GitHub Actions do not
already give, and it costs an engine to host, a pre-1.0 API, and new failure modes. Do not adopt Dagger now.
Take the fixes that the spike found; they are useful with or without Dagger.

| Item | Works with Dagger | Wins over plain | Loses to plain |
|---|---|---|---|
| PR checks | Yes | One command runs all four in parallel; results reused when nothing changed | Rebuilds hydra from the tree in each fresh engine; no commit-message lint |
| Hydra image | Yes, amd64 and arm64 | About 20% faster cold build | On GHA every run is cold (~210 s to build and push vs 3–7 s for warm buildx without `--load`); 73 s to load into Docker |
| ycsb image | Yes, after two fixes | `--repo`/`--branch` as arguments | Nothing over `docker build --build-arg`; and the image turned out to be unused, so it was dropped |
| `container-use` | No: its pinned v0.18 engine crashes on this host and evicts the v0.21 engine | | A 4 s `uv sync` per worktree |

## Setup

| | Value |
|---|---|
| Dagger CLI and engine | v0.21.11 (2026-10-21), engine image `registry.dagger.io/engine:v0.21.11`, 1.05 GB |
| Dagger 1.0 | `v1.0.0-beta.1` (2026-05-29) to `v1.0.0-beta.27` (2026-11-10): prerelease tags only |
| `container-use` | v0.4.2 (2025-08-19); last commit 2026-08-12 |
| Docker / buildx | 29.9.2 / v0.38.1 |
| GHA runners | `ubuntu-24.04` (amd64) and `ubuntu-24.04-arm` (arm64), free on the public fork |

The spike code:

- `.dagger/src/sct_ci/main.py` (99 lines) and `dagger.json`: the functions `hydra-tag`, `hydra`, `hydra-oci`,
  `check`, and `checks`. The `ycsb` function was removed with `docker/ycsb` (section 3).
- `.github/workflows/spike-e7-dagger.yaml`: the same work with plain buildx and with Dagger, side by side.

All times are wall-clock seconds, one run each. "Cold" means no cache for that tool. "Warm" means the same command
again with no change.

## 1. PR checks

The four checks of the root `Jenkinsfile`: pre-commit, unit tests, pytest collect-only, `lint-pipelines`.

| Local (laptop) | hydra.sh, as Jenkins runs it | Dagger `checks` |
|---|---|---|
| pre-commit | 52 | 102 (alone) |
| unit tests (8,520) | 164 | 174 (alone) |
| collect-only (664 tests) | 27 | 24 (alone) |
| `lint-pipelines` (1,154) | 94 | 29 (alone) |
| All four | 337 (one after another) | **214** (in parallel) |
| Again, no change | 337 | **4** (cached result) |
| Again, after a docs-only edit | 337 | 210 (all four run again) |

| GitHub Actions | Plain: 4 parallel jobs in the published hydra image | Dagger: one job |
|---|---|---|
| Longest job | unit tests, 254 (with `--init`) | 343–522, incl. building hydra from the tree in an empty engine |
| Result | All four pass (run 3) | All four pass (runs 2 and 3) |

Findings:

- **Results are cached, failures too.** An unchanged tree returns the last result in 4 s. That includes a failure,
  and it would include a flaky pass. Each check would need a cache-busting input to force a re-run.
- **The whole tree is one input.** A docs-only change re-runs all four checks. Plain CI has the same behaviour, so
  this is no loss, but the "only what changed" promise needs per-check input filters that we would maintain.
- **Git is not in the box.** A worktree's `.git` points outside the tree, so the function makes a fresh repository.
  `check-added-large-files` then failed on `uv.lock` (every file is "new") until the function committed the tree
  first. The commit-message lint (`scripts/lint_commits.sh`) needs the PR history and is not done.
- **The plain path needs `--init`.** With `docker run … bash -c 'python …'`, bash execs Python as PID 1. Python never
  reaps orphans, and `test_remote_long_running` waits forever on a zombie's `/proc` entry (reproduced locally, fixed
  with `--init`). Dagger runs an init by default. hydra.sh keeps bash as PID 1, so Jenkins is not affected.
- **hydra.sh needs a TTY unless `BUILD_TAG`, `JENKINS_URL`, or `GITHUB_ACTIONS` is set.** Outside CI and without a
  terminal it fails with "cannot attach stdin to a TTY-enabled container".

Compared with experiment 4 (plain GHA): GHA alone gives the parallelism, in the published image, with no new tool.
Dagger's extra is result reuse, which matters only for re-runs of an unchanged tree.

## 2. Hydra image

### Hash tag

`hydra-tag` returns `h-` + the first 12 hex digits of `sha256sum Dockerfile pyproject.toml uv.lock | sha256sum`. The
Python and the bash one-liner agree (`h-95ce855c1a88` before the Dockerfile fix, `h-28ededa1d3c5` after), and the
tags published on GHCR match. The tag needs no Dagger.

### Cache

| Local | Plain `docker build` | Dagger `hydra sync` |
|---|---|---|
| Cold | 202 | **166** |
| Warm | **1** | 3 (after the GC fix) |
| `pyproject.toml` changed | 134 | **105** |
| Load the image into Docker | not needed | 73 |
| Build context sent | 899 kB (BuildKit sends only the `ADD`ed files) | the 3 input files |

| GitHub Actions, per arch | buildx | Dagger |
|---|---|---|
| Cold, no cache export, amd64 / arm64 | 211 / 199 | 161 / 165 |
| Cold, with `type=gha` cache export | 284 / 354 | (no equivalent) |
| Warm, same runner | not measured | 2 / 3 |
| **Warm, next run** | **83 / 62**, then **56 / 79** (GHA cache, incl. `--load`) | **193 / 193**, then **190 / 222**: a fresh runner has an empty engine |
| Registry cache (`type=registry`, `mode=max`), cold / warm, amd64 | 246 / 67 | (no equivalent in v0.21) |
| Registry cache, cold / warm, arm64 | 232 / 69 | |
| Of a warm build: `--load` into Docker | ~50 | n/a |
| **Warm, no `--load`: build and push by digest**, GHA cache, amd64 / arm64 | **7 / 3** | 213 / 208 (build 176 / 171, push 37) |
| Warm, no `--load`, registry cache, amd64 / arm64 | **4 / 4** | |
| Pull the pushed digest back for the `readelf` check | 32–45 | n/a (checked in the engine, 3–4) |
| Whole job, warm, no `--load`, with the check (GHA / registry cache) | 72 / 60, 66 / 65 | 227 / 223 |
| Push by digest | 4–19 | 35–43 |

Findings:

- **The engine's default GC defeats the cache on a full disk.** The default keeps 20% of the disk free
  (`minFreeSpace` 149 GB here) and prunes down to `reservedSpace` (10 GB) after each session. On this 93% full disk
  every warm build ran cold (157 s) until `~/.config/dagger/engine.json` set `minFreeSpace` to 10 GB. Jenkins
  builders with full disks would hit the same trap. Plain Docker kept its cache on the same disk.
- **No cache between CI runs.** Each GHA runner starts an empty engine, so every run is cold. Keeping the cache needs
  a persistent engine (self-hosted, see below) or Dagger Cloud. Plain buildx uses `type=gha` with no new service.
- **Dagger v0.21 dropped its remote cache.** `_EXPERIMENTAL_DAGGER_CACHE_CONFIG` (`type=gha`, `type=registry`) worked
  up to v0.20.8. v0.21.0 removed the BuildKit solver backend (dagger#11856); the v0.21.11 CLI still sends the setting
  and the engine ignores it without an error. The Helm chart keeps a `magicache` (Dagger Cloud) setting, but no engine
  code uses it in v0.21.11 (not tried with a token).
- **Most of a warm buildx build was `--load`.** About 50 s of each warm build sent the ~3 GB image into the runner's
  Docker. Without it, a warm build and push by digest took 3–7 s with either cache.
- **A registry cache is as fast as `type=gha` and can be shared with Jenkins.** With `--load`, `type=registry` on
  `ghcr.io/…:cache-<arch>` looked about 12 s slower (67 / 69 s against 56 / 51 s); without it both took 3–7 s. It has
  no 10 GB cap, fork PRs can read it, and the Jenkins builders and laptops can use the same cache.
- **Checking the pushed digest moves the cost.** Pulling the image back to run `readelf` took 32–45 s, close to what
  `--load` cost, so the job only went from 51–79 s to 60–72 s. The PR check jobs pull the PR's hydra image anyway;
  running `readelf` there (~1 s) would leave the build job at about 15 s (not tested).
- **`type=gha` silently does nothing in a `run:` step.** It needs `ACTIONS_RUNTIME_TOKEN` and `ACTIONS_RESULTS_URL`,
  which only JavaScript actions get. The first two runs exported no cache and gave no error. `docker/build-push-action`
  fixes it.
- **Images live in the engine.** A Dagger-built image is not in `docker images`; `export-image` took 73 s for 3 GB.
  hydra.sh needs it in Docker, so local use pays this on each new tag.

### Multi-arch

- QEMU on the laptop: the arm64 build was still in the `python_packages` stage after 8 min and was stopped.
- Native runners: each arch builds in its own job, pushes by digest, and a merge job joins the digests with
  `docker buildx imagetools create`, as the dtest image job does on Jenkins and as scylladb/cql-stress does on GHA.
  Both tools produced `ghcr.io/fruch/sct-hydra-spike:h-28ededa1d3c5-{buildx,dagger}` with `linux/amd64` and
  `linux/arm64`.
- **The Dockerfile downloaded amd64 `kubectl`, `eksctl`, and `helm` for every arch.** An arm64 image built fine and
  was broken. Fixed with `TARGETARCH` (commit `build(hydra)`); the workflow now checks each binary with `readelf`.
  Dagger's `dockerBuild` uses BuildKit's own Dockerfile frontend, so `TARGETARCH` works the same.

### Two registries

`imagetools create` takes several `-t` targets and copies the blobs across registries, so one build publishes to GHCR
and Docker Hub. hydra.sh now reads `HYDRA_IMAGES` (default `docker.io/scylladb/hydra ghcr.io/scylladb/hydra`): it uses a
local copy from any of them, else pulls from each in turn and fails only when all fail (commit `improvement(hydra)`,
with unit tests). On the fork the Docker Hub copy needs the `DOCKERHUB_IMAGE` variable and `DOCKERHUB_*` secrets.
`ghcr.io/scylladb/hydra` does not exist yet: the fallback helps only once the upstream build publishes there too.

## 3. Helper image: ycsb

`docker/ycsb` did not build at all, with either tool:

1. `FROM openjdk:8`: the image was removed from Docker Hub.
2. `mvn -pl cassandra`: scylladb/YCSB master dropped the `cassandra` module. SCT test cases use only `dynamodb` (31)
   and `scylla` (6).

With both fixed locally (`eclipse-temurin:8-jdk-jammy`, scylla and dynamodb only):

| Local | Plain `docker build` | Dagger `ycsb sync` |
|---|---|---|
| Cold | 152 | 146 |
| Warm | 1 | 3 |

- Both tools cache the `RUN git clone` layer by its command string, so a warm build keeps an old YCSB after the branch
  moves. A fix exists in both: Dagger `dag.git(repo).branch(b).tree()`, or BuildKit's `ADD <git-url>#<branch>`. Both
  key the cache by commit.
- Dagger's error for the missing base image was `InvalidResultError … check if the returned value matches the
  function's declared return type`, which points the wrong way.

The image had rotted unnoticed because nothing uses it: SCT runs `docker.io/scylladb/ycsb:1.3.2`
(`defaults/docker_images/ycsb/values_ycsb.yaml`), built in the scylladb/YCSB repository. Commit `build(ycsb)` drops
`docker/ycsb`. The lesson for the other `docker/` images: each one either has a build that runs (a scheduled
`buildx bake` and a GHA matrix, D6 in `spec.md`) or is deleted.

## 4. container-use

**Did not work on this host.** Two headless Claude sessions (container-use tools only, a throwaway clone, the hydra
image as base) ended with the MCP server timing out on connect after 30 s. Cost: $0.32, no change made.

- container-use v0.4.2 embeds Dagger v0.18.14 and starts its own engine `dagger-engine-v0.18.14` (16 months old).
  That engine crash-loops on this host: its CNI setup needs the legacy `iptables` `nat` table, which the 7.0 kernel
  does not have.
- Starting it **removed the v0.21.11 engine and its cache volume**: the v0.18 docker driver garbage-collects every
  other `dagger-engine-*` container as "left over from an older version" (`engine/client/drivers/docker.go`). Two
  Dagger versions on one host evict each other.
- Pointing it at the newer engine (`_EXPERIMENTAL_DAGGER_RUNNER_HOST`) gave no MCP reply in 90 s and no error.
- The project is idle: no release since 2025-08-19, last commit 2026-08-12.

The baseline it would replace is cheap: a fresh `.venv` for a new worktree takes 4 s (`uv sync --frozen`, warm uv
cache). The worktree + `.venv` + `.envrc` flow stays.

## 5. Versions, hosting, API risk

- **Two lines of releases.** Stable is v0.21.x. `v1.0.0-beta.N` are prerelease tags; the main-branch docs already
  describe the 1.0 CLI (`dagger check`, `DAGGER_ENGINE`).
- **Breaking changes:** v0.18.6, v0.18.11, v0.18.17, v0.19.0, and v0.20.1 had "Breaking Changes" sections, 5 in
  18 months, two in patch releases. The CLI and the engine versions must match (enforced since v0.16).
- **Hosting.** The engine runs as a privileged container; rootless is not supported. Options:
  - one engine per builder (`docker-container://`, the default): no network exposure, cache per builder, and the GC
    must be configured for the builder's disk;
  - a shared engine on `tcp://HOST:1234`: **no authentication**, anyone who can reach the port runs privileged
    containers;
  - a shared engine on `tls://HOST:1234?ca_path=…&cert_path=…&key_path=…`: mTLS, supported by v0.21.11 (engine flags
    `--tlscert`, `--tlskey`, `--tlscacert`), a CA and client certificates to run;
  - `kube-pod://` (Kubernetes RBAC), or Dagger Cloud (billed, and the traces go to Dagger).
- **Telemetry.** The CLI sends anonymous telemetry unless `DO_NOT_TRACK=1`, and prints a Dagger Cloud prompt unless
  `DAGGER_NO_NAG=1`.
- **Local git config.** Each call warns `failed to open git repository err="branch config: invalid merge"`: go-git
  rejects `branch.*.merge = refs/pull/N/head`, which two old local branches have. Harmless here.

## Maintenance effort

| | Dagger | Plain |
|---|---|---|
| New code | 111 lines of Python, `dagger.json`, a generated SDK on each machine | the workflow YAML only |
| New infrastructure | an engine per builder or a shared engine with mTLS, its GC config, its disk | none |
| Upgrades | CLI and engine in lock step; a breaking change every few months | buildx and action pins |
| Who can debug it | needs Dagger knowledge (engine GC, cache keys, `noCache` host dirs) | any Docker user |

## Recommendation

Do not adopt Dagger for SCT now. Revisit when Dagger 1.0 is stable and when SCT has a persistent build engine for
another reason.

Adopt these, without Dagger:

1. The hash tag for the hydra image (experiment 5), computed in bash.
2. Native per-arch builds on GHA, pushed by digest and merged with `imagetools`, with the `TARGETARCH` fix and the
   `readelf` check.
3. One build, two registries (GHCR and Docker Hub), and the `HYDRA_IMAGES` fallback in hydra.sh.
4. `docker/build-push-action` (not `docker buildx build` in `run:`) for the build cache, with one `type=registry`
   cache per arch that GHA and the Jenkins builders share, written only from master, and no `--load` in CI: push by
   digest, and run the `readelf` check in the PR check jobs, which pull the image anyway.
5. `--init` for any plain `docker run` of the SCT tests.
6. Drop `docker/` images nothing uses (done for `docker/ycsb`), and build the rest on a schedule, so a removed base
   image or module fails a job, not a person.
7. The PR checks as plain parallel GHA jobs (experiment 4).

Keep the spike branch as the reference for a later look at Dagger 1.0.
