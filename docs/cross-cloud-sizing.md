# Cross-Cloud Instance Sizing

Describe your hardware needs once. SCT picks the right instance on every cloud.

```yaml
instance_type_db:
  vcpu: 16
  memory: 64
```

This resolves to `i8g.4xlarge` on AWS, `z3-highmem-16` on GCE, `Standard_L16s_v4` on Azure, and `DenseIO.E5.Flex` on OCI — automatically.

## Why Use This

- **One config, all clouds** — no more maintaining separate `instance_type_db` / `gce_instance_type_db` / `azure_instance_type_db` / `oci_instance_type_db`.
- **Cost-optimized** — the resolver picks the cheapest instance that satisfies your constraints from preferred families.
- **Backward compatible** — literal strings like `instance_type_db: 'i4i.4xlarge'` still work unchanged.

## Quick Start

### 1. Write constraints in your test config

```yaml
# test-cases/longevity/my-test.yaml
instance_type_db:
  vcpu: 8
  memory: 32

instance_type_loader:
  vcpu: 4

instance_type_monitor:
  vcpu: 2
```

### 2. Preview what resolves

```bash
uv run sct.py sizing preview test-cases/longevity/my-test.yaml
```

Output shows resolved instances per cloud with pricing:

```
Role: db  Constraints: {vcpu: 8, memory: >=32}
  AWS:   i8g.2xlarge       (8 vCPU, 64 GB, 1875 GB NVMe, arm64)    $0.62/hr
  GCE:   z3-highmem-8      (8 vCPU, 64 GB, 750 GB SSD, x86_64)
  Azure: Standard_L8s_v4   (8 vCPU, 64 GB, 1788 GB NVMe, x86_64)
  OCI:   DenseIO.E5.Flex   (8 vCPU, 96 GB, NVMe, x86_64)          $0.82/hr
```

### 3. Run your test

```bash
uv run sct.py run-test longevity_test.LongevityTest.test_custom_time \
  --backend aws --config test-cases/longevity/my-test.yaml
```

The resolver runs at config load time — by the time provisioning starts, instance types are concrete strings.

## Constraint Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vcpu` | int or `"min-max"` | **Yes** | Number of vCPUs. Plain int = exact match. Range = flexible. |
| `memory` | int or string | No | RAM in GB. Plain int = minimum. Supports `>`, `>=`, `<`, `<=`, ranges. |
| `disk` | int or string | No | Local disk in GB. Same operators as memory. DB roles auto-require local disk. |
| `arch` | string | No | `arm64` (or `arm`), `x86_64` (or `x86`). Default: `arm64` on AWS, `x86_64` elsewhere. |

### Examples

```yaml
vcpu: 8              # exactly 8 vCPUs
vcpu: "8-16"         # between 8 and 16 (useful for cross-cloud compatibility)
memory: 32           # at least 32 GB
memory: ">64"        # more than 64 GB
disk: "500-2048"     # between 500 GB and 2 TB local disk
arch: x86            # force x86_64 (shorthand accepted)
```

## How Selection Works

1. **Filter by cloud** — only instances for the active backend
2. **Filter by architecture** — per constraint or cloud default
3. **Filter by preferred family** — each role has ranked families per cloud
4. **Apply constraints** — remove instances that don't satisfy vcpu/memory/disk
5. **Sort** — family rank → price (cheapest) → vCPU (smallest)
6. **Pick first** — deterministic, same input always produces same output

### Preferred Families

| Role | AWS | GCE | Azure | OCI |
|------|-----|-----|-------|-----|
| db | i8g, i7i, i4i | z3-highmem | Standard_L*s_v4 | DenseIO.E5.Flex |
| loader | c6i | e2-standard | Standard_F*s_v2 | VM.Standard3.Flex |
| monitor | t3, m6i | n2-highmem | Standard_D*_v4 | VM.Standard.E4.Flex |

Configured in `data/instance_catalog/sizing_config.yaml`.

### Implicit Constraints

- **DB roles** (`db`, `db_oracle`, `zero_token`): automatically require `local_disk_count > 0`
- **AWS DB**: defaults to `arm64` architecture (i8g family). Override with `arch: x86_64` to get i7i/i4i.
- **Loader/Monitor**: do NOT default to ARM — always x86_64 unless explicitly set.

## Environment Variables

Use SCT's dot-notation to override constraints from the command line:

```bash
export SCT_INSTANCE_TYPE_DB.VCPU=16
export SCT_INSTANCE_TYPE_DB.MEMORY=64
export SCT_INSTANCE_TYPE_DB.ARCH=arm64
```

This builds the dict `{vcpu: 16, memory: 64, arch: "arm64"}` using the same mechanism as other nested SCT parameters.

## CLI Commands

All commands are under `sct.py sizing`:

| Command | Description |
|---------|-------------|
| `sizing preview <config>` | Show how a config resolves across all clouds |
| `sizing resolve --vcpu N [--memory M] [--role R]` | Resolve a single constraint set |
| `sizing catalog --cloud C [--role R] [--family F]` | Browse instance catalog with prices |
| `sizing update-catalog --cloud C` | Regenerate catalog from live cloud APIs |

### Examples

```bash
# Preview a test config
uv run sct.py sizing preview test-cases/longevity/longevity-100gb-4h.yaml

# Resolve constraints interactively
uv run sct.py sizing resolve --vcpu 8 --role db
uv run sct.py sizing resolve --vcpu 8-16 --memory ">60" --role db

# Browse catalog
uv run sct.py sizing catalog --cloud aws --role db
uv run sct.py sizing catalog --cloud oci --family DenseIO.E5

# Refresh pricing from cloud APIs
uv run sct.py sizing update-catalog --cloud all
```

## Migration Guide

### From literal instance types

**Before** (one backend per line):
```yaml
instance_type_db: 'i4i.2xlarge'
gce_instance_type_db: 'z3-highmem-8'
azure_instance_type_db: 'Standard_L8s_v4'
```

**After** (single constraint, all backends):
```yaml
instance_type_db:
  vcpu: 8
  memory: 32
```

### Steps

1. Note the vCPU/memory of your current instance type
2. Write the constraint dict
3. Run `sizing preview` to verify resolution matches
4. Remove cloud-specific parameters (`gce_instance_type_db`, etc.)

### Cross-cloud compatibility with vcpu ranges

OCI DenseIO starts at 16 vCPUs. If your test needs 8 vCPUs on AWS/GCE but OCI has no 8-vCPU option:

```yaml
instance_type_db:
  vcpu: "8-16"
  memory: 32
```

This picks 8 vCPU on AWS/GCE/Azure and 16 vCPU on OCI (smallest available).

## Supported Roles

| Parameter | Role | Notes |
|-----------|------|-------|
| `instance_type_db` | db | Requires local disk |
| `instance_type_db_oracle` | db_oracle | Requires local disk |
| `zero_token_instance_type_db` | zero_token | Requires local disk |
| `instance_type_loader` | loader | Compute-optimized |
| `instance_type_monitor` | monitor | Minimal resources |

## Backend Support

| Backend | Behavior |
|---------|----------|
| `aws`, `gce`, `azure`, `oci` | Full constraint resolution |
| `k8s-eks` | Resolves using AWS catalog |
| `k8s-gke` | Resolves using GCE catalog |
| `docker`, `baremetal`, `k8s-local-kind` | Skipped (logged at INFO level) |

## Error Handling

When no instance matches:

```
NoMatchingInstanceError: No instance found for role='db', cloud='oci' satisfying:
  vcpu == 8, memory_gb >= 128
  Candidates checked: 6 (from families: DenseIO.E5)
```

This means you need to adjust constraints or use a literal for that cloud.

## Catalog Files

```
data/instance_catalog/
├── aws.yaml              # Instance specs + pricing
├── gce.yaml
├── azure.yaml
├── oci.yaml
└── sizing_config.yaml    # Preferred families, role constraints, sort order
```

Regenerate with `uv run sct.py sizing update-catalog --cloud all`.
