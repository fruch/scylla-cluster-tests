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
┌─────────────────────────────────────────────────────────────────────┐
│  SCT Test Run                                                       │
│                                                                     │
│  1. Config loaded → is_minicloud_active() == True                   │
│  2. get_cluster_aws() entry point                                   │
│     ├── start_minicloud()                                           │
│     │   ├── Find/verify minicloud binary (pinned version)           │
│     │   ├── Run minicloud-setup.sh (creates bridges, TAP)           │
│     │   ├── Start minicloud process (subprocess, port 5000)         │
│     │   ├── Wait for health check (HTTP GET / retry loop)           │
│     │   └── Set AWS_ENDPOINT_URL=http://localhost:5000              │
│     ├── [normal AWS cluster setup — all calls go to minicloud]      │
│     ├── [test executes — SSH to VMs, CQL, stress, etc.]            │
│     └── teardown                                                    │
│         ├── TerminateInstances → minicloud kills QEMU processes     │
│         └── stop_minicloud()                                        │
│             ├── Run minicloud-setup.sh --cleanup (remove TAPs)      │
│             ├── Kill minicloud process (SIGTERM → SIGKILL)          │
│             └── Clean cached state if requested (NOT AMI cache)     │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  SCT Resource Cleanup (collect-logs / clean-resources)               │
│                                                                     │
│  minicloud MUST be running during cleanup so TerminateInstances     │
│  can reach it. Two scenarios:                                       │
│                                                                     │
│  A) Same SCT run (teardown within test):                            │
│     minicloud is still running → TerminateInstances works normally  │
│                                                                     │
│  B) Separate cleanup run (e.g. Jenkins post-build, manual cleanup): │
│     ├── Detect minicloud_endpoint_url in saved test config          │
│     ├── start_minicloud() (re-attach to existing state dir)         │
│     ├── TerminateInstances for orphaned VMs                         │
│     └── stop_minicloud()                                            │
└─────────────────────────────────────────────────────────────────────┘
```

**When minicloud starts**: At the beginning of `get_cluster_aws()`, before any EC2 API calls. This ensures the endpoint is ready before VPC/subnet creation.

**When minicloud stops**: During test teardown (`TearDown` / `destroy()` flow), AFTER `TerminateInstances` has been called. The order is critical — minicloud must be alive to receive the terminate call. Only after VMs are terminated (or terminate fails gracefully) do we stop the minicloud process itself.

**Cleanup with minicloud**: SCT's resource cleanup (`clean-resources` command, Jenkins post-build cleanup) must also be able to terminate minicloud VMs. This requires minicloud to be running during cleanup. The `MinicloudManager` supports re-attaching to an existing minicloud state directory so that a cleanup run can discover and terminate leftover VMs from a previous failed test.

**AMI caching**: minicloud caches downloaded AMI images in `~/.cache/minicloud/` (or configurable path). First run downloads via EBS Direct API (requires AWS credentials). Subsequent runs reuse the cache — no download needed. AMI cache is NEVER cleaned during teardown — it's expensive to re-download.

**`TerminateInstances` in minicloud**: This is the **first prerequisite** to implement in minicloud before SCT integration can be fully clean. See [minicloud issue tracking](https://github.com/scylladb/minicloud/issues). The implementation in minicloud should:
- Send SIGTERM to the QEMU process for the instance
- Wait for graceful shutdown (timeout 30s), then SIGKILL
- Clean up TAP device and bridge membership for the instance
- Return standard EC2 `TerminateInstances` response

Until `TerminateInstances` is implemented, the fallback is: SCT catches the error in teardown (minicloud-only), logs a warning, and `stop_minicloud()` kills the entire minicloud process (which kills all QEMU child processes). This is acceptable for development but not for CI where cleanup reliability matters.

**Definition of Done**:
- [ ] `is_minicloud_active()` returns True when `AWS_ENDPOINT_URL` or `minicloud_endpoint_url` is set
- [ ] `start_minicloud()` launches the process, sets up networking, waits for health
- [ ] `stop_minicloud()` kills the process and cleans up networking
- [ ] Health check runs at test start and gives actionable error if minicloud is unreachable
- [ ] Unit test validates detection, lifecycle, and health check logic

**Dependencies**: None

---

### Phase 2: Implement TerminateInstances in minicloud + adapt AWSCluster — Importance: HIGH

**Objective**: Implement `TerminateInstances` in minicloud (upstream), then guard the remaining known gaps in SCT so the AWS backend works transparently.

**Implementation**:

**Part A — minicloud upstream (prerequisite)**:
- Implement `TerminateInstances` in minicloud ([scylladb/minicloud](https://github.com/scylladb/minicloud)):
  - Parse `InstanceId.N` parameters from the EC2 Query API request
  - For each instance: send SIGTERM to QEMU process → wait 30s → SIGKILL
  - Clean up instance's TAP device and bridge membership
  - Remove instance from internal state (so `DescribeInstances` no longer returns it)
  - Return standard EC2 `TerminateInstancesResponse` XML
- This unblocks clean teardown and resource cleanup in SCT

**Part B — SCT guards for remaining limitations**:
- When `is_minicloud_active()`:
  - Force `instance_provision: "on_demand"` — skip spot, capacity reservation, dedicated host, placement group logic
  - Skip EIP allocation/association (minicloud VMs are reachable via private IP from host after `minicloud-setup.sh`)
  - If `TerminateInstances` is not yet available (version check), catch the error gracefully and rely on `stop_minicloud()` killing all QEMU processes
- These are minimal guards in existing code paths, not new backend classes

**Definition of Done**:
- [ ] `TerminateInstances` works in minicloud — QEMU process is killed, instance disappears from `DescribeInstances`
- [ ] `AWSCluster._create_on_demand_instances` successfully calls minicloud's `RunInstances`
- [ ] Spot/capacity-reservation/EIP logic is skipped when minicloud is active
- [ ] Teardown calls `TerminateInstances` normally (minicloud handles it); fallback to process kill if unsupported
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

**Objective**: Add a Jenkins job that runs the AMI artifact test against minicloud on a standard AWS SCT runner (bare-metal `.metal` instance with KVM support), installing KVM and minicloud as part of the pipeline.

**Implementation**:
- Create `jenkins-pipelines/oss/artifacts-minicloud.jenkinsfile`:
  ```groovy
  #!groovy
  // Jenkins pipeline for AMI artifact test via minicloud (no cloud instance costs for Scylla VMs)
  // Runs on a standard AWS SCT runner — installs KVM + minicloud during setup

  def call() {
      pipeline {
          agent { label 'sct-runner' }  // Standard SCT runner (AWS .metal instance)

          stages {
              stage('Setup KVM and minicloud') {
                  steps {
                      // Install KVM/QEMU if not already present
                      sh '''
                          if ! command -v kvm-ok &>/dev/null || ! kvm-ok; then
                              echo "Installing KVM/QEMU packages..."
                              sudo apt-get update
                              sudo apt-get install -y --no-install-recommends \
                                  qemu-kvm qemu-system-x86 libvirt-daemon-system \
                                  bridge-utils iproute2
                              sudo modprobe kvm
                              sudo modprobe kvm_intel || sudo modprobe kvm_amd || true
                          fi
                          # Verify KVM is available
                          test -e /dev/kvm || (echo "ERROR: /dev/kvm not available — need bare-metal instance" && exit 1)
                          sudo chmod 666 /dev/kvm
                      '''
                      // Install minicloud (pinned version)
                      sh '''
                          scripts/install-minicloud.sh
                      '''
                  }
              }
              stage('Run artifact test') {
                  steps {
                      // SCT manages minicloud lifecycle (start/stop) automatically
                      sh '''
                          ./docker/env/hydra.sh run-test \
                              artifacts_test.ArtifactsTest.test_scylla_service \
                              --backend aws \
                              --config test-cases/artifacts/ami-minicloud.yaml
                      '''
                  }
              }
              stage('Collect logs') {
                  steps {
                      // minicloud must be running for collect-logs to reach VMs
                      // SCT handles this: starts minicloud if not running, then collects
                      sh '''
                          ./docker/env/hydra.sh collect-logs \
                              --backend aws \
                              --config test-cases/artifacts/ami-minicloud.yaml
                      '''
                  }
              }
              stage('Clean resources') {
                  steps {
                      // minicloud must be running for TerminateInstances to work
                      // SCT starts minicloud, terminates VMs, then stops minicloud
                      sh '''
                          ./docker/env/hydra.sh clean-resources \
                              --backend aws \
                              --config test-cases/artifacts/ami-minicloud.yaml
                      '''
                  }
              }
          }
          post {
              always {
                  // Final safety net: ensure no orphan minicloud/QEMU processes
                  sh 'pkill -f minicloud || true'
                  sh 'pkill -f "qemu-system" || true'
                  sh 'sudo scripts/minicloud-setup.sh --cleanup || true'
              }
          }
      }
  }
  ```

- **Critical: minicloud must be alive during cleanup stages**. The `collect-logs` and `clean-resources` stages need to reach VMs (SSH) and terminate them (`TerminateInstances`). SCT's `MinicloudManager` handles this — if minicloud is not running but `minicloud_endpoint_url` is configured, it re-starts minicloud (re-attaching to existing state directory) before performing cleanup operations.

- **Runner requirements — standard AWS SCT runner with `.metal` instance type**:
  - AWS `.metal` instance (e.g. `i3.metal`, `m5.metal`, `c5.metal`) — provides bare-metal KVM access
  - Alternatively: any instance with nested virtualization enabled (`.metal` guarantees it)
  - At minimum: 4 vCPU, 32 GiB RAM, 100 GiB disk (for AMI cache + VM overhead + SCT itself)
  - The minicloud `--lightweight` mode (1 vCPU + 1.5 GiB per VM) makes resource usage minimal
  - AWS credentials available (for minicloud's proxy passthrough to real AWS APIs)
  - Standard `sct-runner` label — no special label needed since KVM is installed in-pipeline

- Add `scripts/install-minicloud.sh`:
  ```bash
  #!/bin/bash
  set -euo pipefail

  MINICLOUD_VERSION="${MINICLOUD_VERSION:-v0.3.0}"
  MINICLOUD_REPO="scylladb/minicloud"
  INSTALL_DIR="/usr/local/bin"

  # Check if already installed at correct version
  if command -v minicloud &>/dev/null; then
      installed=$(minicloud --version 2>/dev/null || echo "unknown")
      if [[ "$installed" == *"$MINICLOUD_VERSION"* ]]; then
          echo "minicloud $MINICLOUD_VERSION already installed"
          exit 0
      fi
  fi

  echo "Installing minicloud $MINICLOUD_VERSION..."

  # Download pre-built binary from GitHub releases (preferred)
  # Falls back to building from source if no release binary available
  RELEASE_URL="https://github.com/${MINICLOUD_REPO}/releases/download/${MINICLOUD_VERSION}/minicloud-linux-amd64"
  if curl -fsSL --head "$RELEASE_URL" &>/dev/null; then
      sudo curl -fsSL -o "${INSTALL_DIR}/minicloud" "$RELEASE_URL"
      sudo chmod +x "${INSTALL_DIR}/minicloud"
  else
      echo "No pre-built binary found, building from source..."
      # Requires Rust toolchain
      command -v cargo &>/dev/null || curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
      source "$HOME/.cargo/env"
      TMPDIR=$(mktemp -d)
      git clone --depth 1 --branch "$MINICLOUD_VERSION" \
          "https://github.com/${MINICLOUD_REPO}.git" "$TMPDIR/minicloud"
      cd "$TMPDIR/minicloud" && cargo build --release
      sudo cp target/release/minicloud "${INSTALL_DIR}/minicloud"
      rm -rf "$TMPDIR"
  fi

  # Copy networking setup script
  sudo cp scripts/minicloud-setup.sh "${INSTALL_DIR}/minicloud-setup.sh"
  sudo chmod +x "${INSTALL_DIR}/minicloud-setup.sh"

  echo "minicloud installed: $(minicloud --version)"
  ```

- **Trigger**: Can run on every PR that touches `artifacts_test.py`, `sdcm/cluster_aws.py`, or AMI-related code. Also runs nightly as a sanity check.

**Definition of Done**:
- [ ] Jenkins pipeline exists and can be triggered manually
- [ ] Pipeline installs KVM and minicloud on a standard AWS SCT runner
- [ ] Pipeline runs the artifact test, collects logs, and cleans resources
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
| Missing `TerminateInstances` causes teardown failures | High | Medium | Implement `TerminateInstances` in minicloud first (Phase 2A prerequisite). Fallback: `stop_minicloud()` kills all QEMU processes. CI `post { always }` block pkills orphans as safety net. |
| minicloud process crashes mid-test | Medium | High | Health check wrapper detects crash and fails test with clear message. Jenkins `post { always }` block ensures cleanup. |
| QEMU NVMe behavior differs from real EC2 NVMe | Medium | Low | Hardware-specific subtests get relaxed/skipped paths when minicloud is active. Not core test value. |
| `AWS_ENDPOINT_URL` accidentally set in production CI | Low | High | Only the dedicated `sct-runner-minicloud` Jenkins label sets this. Health check verifies minicloud is reachable — if not, fail loudly. |
| AMI first-download requires AWS credentials + takes time | Low | Low | One-time cost. Cached on Jenkins runner across builds. Document required IAM permissions. |
| minicloud is under active development — API may change | Medium | Medium | Pin to specific minicloud version. Health check at startup catches incompatibilities early. |
| Jenkins runner doesn't have KVM support | Low | High | Use AWS `.metal` instances (e.g. `i3.metal`) which provide bare-metal KVM access. Pipeline verifies `/dev/kvm` exists and fails loudly if not. KVM + QEMU installed in-pipeline if missing. |
