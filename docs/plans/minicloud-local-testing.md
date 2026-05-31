---
status: draft
domain: testing
created: 2026-03-11
last_updated: 2026-05-31
owner: null
---
# Minicloud Local Testing Integration

## 1. Problem Statement

Running AMI artifact tests (`artifacts_test.py::ArtifactsTest::test_scylla_service`) requires an AWS account, incurs cloud costs ($0.30–$1.00+ per run for `i4i.large` spot instances), and depends on network connectivity to AWS. This creates friction for local development:

- **Cost**: Every test iteration launches a real EC2 instance (even for verifying basic Scylla service behavior)
- **Latency**: Instance provisioning takes 2–5 minutes before the test logic even starts
- **Access barriers**: Developers need AWS credentials, correct IAM permissions, and VPN/network access
- **CI dependency**: Artifact tests can only run in Jenkins pipelines with cloud access, not on developer machines

[minicloud](https://github.com/scylladb/minicloud) is a local AWS EC2 emulator backed by QEMU/KVM that implements a subset of the EC2 Query API. It can launch real Linux VMs from AMI images locally, with NVMe storage, TAP networking, and IMDS metadata — making it a viable backend for running AMI artifact tests without AWS.

### Key design decision: transparent proxy, NOT selective routing

As of [minicloud PR #15](https://github.com/scylladb/minicloud/pull/15), minicloud implements a **generic HTTP proxy passthrough** — any EC2 API action that minicloud doesn't handle locally is automatically SigV4-signed and forwarded to real AWS. This also covers SSM, STS, SecretsManager, and S3.

**This means SCT does NOT need to split its API calls between minicloud and real AWS.** The entire approach is:

```bash
export AWS_ENDPOINT_URL=http://localhost:5000
```

All boto3 calls go to minicloud. Minicloud handles instance lifecycle locally (RunInstances, DescribeInstances, VPC/subnet/SG) and transparently proxies everything else (DescribeImages, DescribeInstanceTypes, SSM GetParameter, STS AssumeRole) to real AWS.

**What this eliminates from the original plan:**
- ~~Centralize EC2 client creation with dual-endpoint factory~~ — not needed
- ~~Selective endpoint routing logic~~ — minicloud handles this transparently
- ~~Migrate scattered boto3 calls to a factory~~ — not needed

## 2. Current State

### Backend selection

Backend is chosen via `cluster_backend` config parameter in `sdcm/sct_config.py`:

```python
cluster_backend: String = SctField(
    description="backend that will be used, aws/gce/azure/oci/docker/xcloud",
)
```

The `init_resources()` method in `sdcm/tester.py` dispatches to backend-specific methods:

```python
if cluster_backend in ("aws", "aws-siren"):
    self.get_cluster_aws(...)
elif cluster_backend == "docker":
    self.get_cluster_docker()
```

### AWS cluster implementation

- `sdcm/cluster_aws.py` — `AWSCluster(cluster.BaseCluster)`: provisions instances via `EC2ClientWrapper`
- `sdcm/cluster_aws.py` — `AWSNode(cluster.BaseNode)`: wraps `ec2.Instance`, provides SSH access
- `sdcm/cluster_aws.py` — `ScyllaAWSCluster(cluster.BaseScyllaCluster, AWSCluster)`: Scylla-specific DB cluster

### How AWS_ENDPOINT_URL works with boto3

boto3 natively supports the `AWS_ENDPOINT_URL` environment variable (added in botocore 1.31.0+). When set, ALL service clients automatically use it. No code changes required in SCT — boto3 routes all calls to minicloud, and minicloud decides what to handle locally vs proxy.

### minicloud capabilities (post PR #15)

- **Instance lifecycle**: `RunInstances`, `DescribeInstances`, `CreateVpc`, `CreateSubnet`, `CreateSecurityGroup`, `CreateKeyPair` — handled locally with QEMU/KVM VMs
- **Transparent proxy**: Any unknown EC2 action (e.g. `DescribeImages`, `DescribeInstanceTypes`) is SigV4-signed and forwarded to real AWS
- **Other AWS services**: SSM, STS, SecretsManager, S3 — all proxied to real AWS via generic HTTP passthrough
- **VMs**: QEMU/KVM with NVMe controllers, TAP networking, IMDS v2 metadata
- **Instance type**: Only `i4i.large` (2 vCPU, 16 GiB RAM; `--lightweight` mode: 1 vCPU, 1.5 GiB)
- **AMI download**: Uses EBS Direct API to download real AMI images on first use (cached)
- **Missing (critical)**: `TerminateInstances` — needs minicloud issue/implementation
- **Not enforced**: Security group rules (stored but not applied in v1)

### AMI artifact test

- `artifacts_test.py` — `ArtifactsTest(ClusterTester)`: main test class
- `test-cases/artifacts/ami.yaml` — config: `cluster_backend: 'aws'`, `instance_type_db: 'i4i.large'`, `n_db_nodes: 1`, `n_loaders: 0`, `n_monitor_nodes: 0`
- Test method `test_scylla_service` verifies: ENA support, IO params, NVMe write cache, XFS discard, snitch, node health, CQL, cassandra-stress, stop/start/restart, housekeeping, perftune, time sync services

## 3. Goals

1. **Run AMI artifact test locally** against minicloud-managed VMs with zero AWS instance costs
2. **Zero SCT code changes for API routing** — minicloud's transparent proxy handles all AWS API forwarding; SCT just sets `AWS_ENDPOINT_URL`
3. **Reuse existing AWS backend code unchanged** — minicloud is transparent; `cluster_backend: 'aws'` works as-is
4. **Minimal SCT changes** — only adapt for known minicloud limitations (no spot, no EIP, graceful TerminateInstances handling, hardware-specific test checks)
5. **Developer experience**: set `AWS_ENDPOINT_URL=http://localhost:5000` and run the test normally

## 4. Implementation Phases

### Phase 1: Add minicloud configuration, lifecycle management, and health check — Importance: HIGH

**Objective**: Add SCT config support for minicloud mode detection, manage the minicloud process lifecycle (start/stop), and provide a pre-flight health check.

**Implementation**:
- Add to `sdcm/sct_config.py`:
  ```python
  minicloud_endpoint_url: String = SctField(
      description="""EC2 API endpoint URL for minicloud. When set (or when AWS_ENDPOINT_URL
          env var points to a minicloud instance), SCT adapts its behavior for known
          minicloud limitations (no spot instances, no EIP, graceful TerminateInstances).
          Example: http://localhost:5000""",
      appendable=False,
  )
  ```
- Add default `minicloud_endpoint_url: ''` in `defaults/test_default.yaml`
- The parameter can also be auto-detected from `AWS_ENDPOINT_URL` env var
- Add `sdcm/utils/minicloud.py` with:
  - `is_minicloud_active() -> bool` — checks if minicloud endpoint is configured
  - `check_minicloud_reachability()` — HTTP health check; raises clear error if minicloud is down
  - `start_minicloud()` — starts the minicloud process, runs `minicloud-setup.sh` for networking
  - `stop_minicloud()` — stops the minicloud process and cleans up VMs/networking
  - `MinicloudManager` class — context manager for the full lifecycle
- Pin minicloud version/commit reference in a constant (e.g. `MINICLOUD_VERSION = "v0.3.0"` or a git SHA)

**Minicloud Process Lifecycle**:

```
┌─────────────────────────────────────────────────────────────────┐
│  SCT Test Run                                                   │
│                                                                 │
│  1. Config loaded → is_minicloud_active() == True               │
│  2. get_cluster_aws() entry point                               │
│     ├── start_minicloud()                                       │
│     │   ├── Find/verify minicloud binary (pinned version)       │
│     │   ├── Run minicloud-setup.sh (creates bridges, TAP)       │
│     │   ├── Start minicloud process (subprocess, port 5000)     │
│     │   ├── Wait for health check (HTTP GET / retry loop)       │
│     │   └── Set AWS_ENDPOINT_URL=http://localhost:5000          │
│     ├── [normal AWS cluster setup — all calls go to minicloud]  │
│     ├── [test executes — SSH to VMs, CQL, stress, etc.]        │
│     └── teardown                                                │
│         ├── [TerminateInstances → graceful failure if missing]  │
│         └── stop_minicloud()                                    │
│             ├── Kill minicloud process (SIGTERM → SIGKILL)      │
│             ├── Run minicloud-setup.sh --cleanup (remove TAPs)  │
│             └── Clean cached state if requested                 │
└─────────────────────────────────────────────────────────────────┘
```

**When minicloud starts**: At the beginning of `get_cluster_aws()`, before any EC2 API calls. This ensures the endpoint is ready before VPC/subnet creation.

**When minicloud stops**: During test teardown (`TearDown` / `destroy()` flow), after instance termination attempts. This ensures VMs are cleaned up even if `TerminateInstances` is not supported.

**AMI caching**: minicloud caches downloaded AMI images in `~/.cache/minicloud/` (or configurable path). First run downloads via EBS Direct API (requires AWS credentials). Subsequent runs reuse the cache — no download needed.

**Definition of Done**:
- [ ] `is_minicloud_active()` returns True when `AWS_ENDPOINT_URL` or `minicloud_endpoint_url` is set
- [ ] `start_minicloud()` launches the process, sets up networking, waits for health
- [ ] `stop_minicloud()` kills the process and cleans up networking
- [ ] Health check runs at test start and gives actionable error if minicloud is unreachable
- [ ] Unit test validates detection, lifecycle, and health check logic

**Dependencies**: None

---

### Phase 2: Adapt AWSCluster for minicloud limitations — Importance: HIGH

**Objective**: Guard the known minicloud gaps so the existing AWS backend works transparently.

**Implementation**:
- When `is_minicloud_active()`:
  - Force `instance_provision: "on_demand"` — skip spot, capacity reservation, dedicated host, placement group logic
  - Skip EIP allocation/association (minicloud VMs are reachable via private IP from host after `minicloud-setup.sh`)
  - Handle missing `TerminateInstances` gracefully — catch the error in teardown **only when minicloud is active**, log warning. The `stop_minicloud()` call in teardown kills the minicloud process which destroys all VMs.
- These are minimal guards in existing code paths, not new backend classes

**Definition of Done**:
- [ ] `AWSCluster._create_on_demand_instances` successfully calls minicloud's `RunInstances`
- [ ] Spot/capacity-reservation/EIP logic is skipped when minicloud is active
- [ ] Teardown doesn't crash on missing `TerminateInstances` (minicloud only — real AWS teardown is never affected)
- [ ] `AWSNode` resolves IP and establishes SSH to the minicloud VM

**Dependencies**: Phase 1

---

### Phase 3: AMI artifact test config and end-to-end validation — Importance: HIGH

**Objective**: Create a test configuration and validate the artifact test runs against minicloud.

**Implementation**:
- Create `test-cases/artifacts/ami-minicloud.yaml`:
  ```yaml
  root_disk_size_db: 50
  backtrace_decoding: false
  cluster_backend: 'aws'
  instance_type_db: 'i4i.large'
  instance_provision: 'on_demand'
  n_db_nodes: 1
  n_loaders: 0
  n_monitor_nodes: 0
  nemesis_class_name: 'NoOpMonkey'
  region_name: 'us-east-1'
  scylla_linux_distro: 'centos'
  test_duration: 60
  user_prefix: 'artifacts-ami-minicloud'
  minicloud_endpoint_url: 'http://localhost:5000'
  ip_ssh_connections: 'private'
  ```
- Usage: `uv run sct.py run-test artifacts_test.ArtifactsTest.test_scylla_service --backend aws --config test-cases/artifacts/ami-minicloud.yaml`
  (SCT starts/stops minicloud automatically based on `minicloud_endpoint_url` being set)
- Validate core subtests: `check_scylla`, `check_cqlsh`, `verify_snitch`, `verify_node_health`, stop/start/restart
- Add minicloud-aware paths for hardware-specific checks:
  - `check ENA support` — QEMU may not expose ENA; skip or alternative check
  - `check Scylla IO Params` — QEMU NVMe may differ; relaxed validation
  - `verify_nvme_write_cache` — may not expose write_cache sysfs
  - `check perftune` — may differ on QEMU; relaxed check

**Definition of Done**:
- [ ] Core subtests pass: Scylla starts, CQL works, cassandra-stress runs, stop/start/restart works
- [ ] Hardware-specific subtests have minicloud-aware paths with clear skip messages
- [ ] Test can run fully offline (no AWS instance costs) once AMI is cached locally

**Dependencies**: Phase 2

---

### Phase 4: Jenkins pipeline for minicloud artifact test — Importance: HIGH

**Objective**: Add a Jenkins job that runs the AMI artifact test against minicloud on a dedicated SCT runner with KVM support.

**Implementation**:
- Create `jenkins-pipelines/oss/artifacts-minicloud.jenkinsfile`:
  ```groovy
  #!groovy
  // Jenkins pipeline for AMI artifact test via minicloud (no cloud instance costs)

  def call() {
      pipeline {
          agent { label 'sct-runner-minicloud' }  // KVM-capable runner

          environment {
              AWS_ENDPOINT_URL = 'http://localhost:5000'
          }

          stages {
              stage('Install minicloud') {
                  steps {
                      // Download pinned minicloud release or build from source
                      sh 'scripts/install-minicloud.sh'
                  }
              }
              stage('Run artifact test') {
                  steps {
                      sh '''
                          ./docker/env/hydra.sh run-test \
                              artifacts_test.ArtifactsTest.test_scylla_service \
                              --backend aws \
                              --config test-cases/artifacts/ami-minicloud.yaml
                      '''
                  }
              }
          }
          post {
              always {
                  // minicloud cleanup is handled by SCT teardown, but ensure no orphans
                  sh 'pkill -f minicloud || true'
                  sh 'scripts/minicloud-setup.sh --cleanup || true'
              }
          }
      }
  }
  ```

- **Runner requirements**:
  - KVM-capable (bare metal or nested virt enabled) — `i4i.large` VM needs 2 vCPU + 16 GiB RAM (or `--lightweight`: 1 vCPU + 1.5 GiB)
  - At minimum: 4 vCPU, 32 GiB RAM, 100 GiB disk (for AMI cache + VM overhead + SCT itself)
  - Linux kernel with TAP/bridge support (standard on SCT runners)
  - AWS credentials available (for minicloud's proxy passthrough to real AWS APIs)
  - Label: `sct-runner-minicloud`

- Add `scripts/install-minicloud.sh`:
  - Downloads pinned minicloud binary (from GitHub releases or builds from pinned commit)
  - Verifies checksum
  - Places binary in `$PATH`
  - Runs `minicloud-setup.sh` for initial network setup

- **Trigger**: Can run on every PR that touches `artifacts_test.py`, `sdcm/cluster_aws.py`, or AMI-related code. Also runs nightly as a sanity check.

**Definition of Done**:
- [ ] Jenkins pipeline exists and can be triggered manually
- [ ] Runner with `sct-runner-minicloud` label is provisioned with KVM support
- [ ] Pipeline installs minicloud, runs the artifact test, and cleans up
- [ ] Pipeline passes consistently (not flaky)
- [ ] AMI cache persists across runs on the runner (no re-download every time)

**Dependencies**: Phase 3

---

### Phase 5: Documentation and developer guide — Importance: MEDIUM

**Objective**: Document minicloud setup, usage, and known limitations.

**Implementation**:
- Create `docs/minicloud-testing.md` covering:
  - Prerequisites (Linux, KVM, QEMU, AWS credentials for proxy, Rust toolchain if building from source)
  - Installing minicloud (`scripts/install-minicloud.sh` or manual build)
  - Running `minicloud-setup.sh` for host connectivity
  - Running the AMI artifact test locally (SCT manages minicloud lifecycle automatically)
  - Manual minicloud management (for debugging): start, stop, check logs
  - First-run AMI download (requires `ebs:ListSnapshotBlocks` + `ebs:GetSnapshotBlock`)
  - AMI cache location and management (`~/.cache/minicloud/`)
  - Known limitations and skipped subtests
  - Troubleshooting (AppArmor on Ubuntu 24.04+, KVM access, port conflicts)
- Update `AGENTS.md` backends section to mention minicloud

**Definition of Done**:
- [ ] A developer can follow the guide from scratch and run the artifact test
- [ ] `docs/minicloud-testing.md` exists with complete setup guide

**Dependencies**: Phase 3

## 5. Testing Requirements

### Unit Tests

| Phase | Test | What it verifies |
|-------|------|-----------------|
| 1 | `test_minicloud_detection_from_env` | `is_minicloud_active()` detects `AWS_ENDPOINT_URL` and `minicloud_endpoint_url` |
| 1 | `test_minicloud_health_check_failure` | Clear error when minicloud is unreachable |
| 1 | `test_minicloud_start_stop_lifecycle` | `MinicloudManager` starts process, waits for health, stops and cleans up |
| 2 | `test_create_instances_skips_spot_for_minicloud` | On-demand is forced when minicloud is active |
| 2 | `test_terminate_graceful_when_unsupported` | Teardown doesn't crash on missing TerminateInstances (minicloud only) |
| 2 | `test_eip_skipped_for_minicloud` | EIP allocation logic is bypassed |

### Integration Tests

| Phase | Test | Service |
|-------|------|---------|
| 3 | `test_minicloud_ami_artifact_e2e` | minicloud (QEMU/KVM) |

Requires minicloud running locally + KVM access. Marked `@pytest.mark.integration` with `skipif` guard.

### Manual Testing

| Phase | Procedure |
|-------|-----------|
| 3 | Run artifact test with minicloud config — verify Scylla starts, CQL works, minicloud starts/stops automatically |
| 3 | Run artifact test WITHOUT `minicloud_endpoint_url` — verify zero behavioral change |
| 4 | Jenkins pipeline runs green on `sct-runner-minicloud` node |

## 6. Success Criteria

- [ ] AMI artifact test core subtests pass against minicloud with zero AWS instance costs
- [ ] SCT manages minicloud lifecycle automatically (start before test, stop after teardown)
- [ ] No selective routing code in SCT — minicloud's transparent proxy handles all forwarding
- [ ] Existing AWS backend tests pass with no behavioral changes when `minicloud_endpoint_url` is unset
- [ ] Jenkins pipeline runs the test on a KVM-capable runner with AMI cache persistence
- [ ] Developer can set up minicloud and run the artifact test locally following the documentation
- [ ] SCT code changes are minimal — only guards for known minicloud limitations

## 7. Risk Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| minicloud proxy doesn't support an API SCT calls during setup | Medium | Medium | minicloud proxies unknown actions to real AWS — so this should "just work". If an action fails, it's a minicloud bug to fix upstream (not an SCT workaround). |
| Missing `TerminateInstances` causes teardown failures | High | Medium | Guard in SCT (minicloud-only). `stop_minicloud()` kills the process which destroys all VMs. File minicloud issue for upstream fix. |
| minicloud process crashes mid-test | Medium | High | Health check wrapper detects crash and fails test with clear message. Jenkins `post { always }` block ensures cleanup. |
| QEMU NVMe behavior differs from real EC2 NVMe | Medium | Low | Hardware-specific subtests get relaxed/skipped paths when minicloud is active. Not core test value. |
| `AWS_ENDPOINT_URL` accidentally set in production CI | Low | High | Only the dedicated `sct-runner-minicloud` Jenkins label sets this. Health check verifies minicloud is reachable — if not, fail loudly. |
| AMI first-download requires AWS credentials + takes time | Low | Low | One-time cost. Cached on Jenkins runner across builds. Document required IAM permissions. |
| minicloud is under active development — API may change | Medium | Medium | Pin to specific minicloud version. Health check at startup catches incompatibilities early. |
| Jenkins runner doesn't have KVM support | Low | High | Provision bare-metal runner or enable nested virt. Verify with `kvm-ok` in pipeline setup stage. |
