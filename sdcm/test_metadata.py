"""Structured metadata model for SCT test-case documentation and labeling.

Metadata is embedded directly in test-case YAML files as a ``test_metadata:`` section
and validated on config load via pydantic. It flows to Argus at test runtime via
``submit_sct_run()``.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

VALID_BACKENDS = {
    "aws",
    "gce",
    "azure",
    "docker",
    "k8s-eks",
    "k8s-gke",
    "k8s-local-kind",
    "baremetal",
    "xcloud",
}


class TestMetadata(BaseModel):
    """Structured metadata for test-case documentation and labeling.

    Embedded directly in test-case YAML files and validated on config load.
    Flows to Argus via sct_config submission.
    """

    description: str | None = Field(
        default=None,
        description="Human-readable description of what this test validates (2-4 sentences).",
    )
    test_type: (
        Literal[
            "longevity",
            "performance",
            "upgrade",
            "artifacts",
            "manager",
            "functional",
            "scale",
            "jepsen",
            "gemini",
            "features",
            "platform-migration",
            "vector-search",
            "cdc",
        ]
        | None
    ) = Field(
        default=None,
        description="Primary test category.",
    )
    tier: Literal["sanity", "tier1", "release", "ondemand"] | None = Field(
        default=None,
        description=(
            "Test priority tier. "
            "sanity: basic smoke tests, run on every commit. "
            "tier1: core regression, run weekly. "
            "release: mandatory during release qualification. "
            "ondemand: run on-demand for investigation or niche scenarios."
        ),
    )
    duration_class: Literal["short", "medium", "long"] | None = Field(
        default=None,
        description="Test duration bucket: short (<6h), medium (6-24h), long (>24h).",
    )
    supported_backends: list[str] | None = Field(
        default=None,
        description=(
            "Backends this test supports. If omitted/None, the test supports ALL backends. "
            "Values: aws, gce, azure, docker, k8s-eks, k8s-gke, k8s-local-kind, baremetal, xcloud."
        ),
    )
    stress_tools: list[str] = Field(
        default_factory=list,
        description="Stress/load tools used: cassandra-stress, scylla-bench, ycsb, latte, gemini, etc.",
    )
    workload: (
        Literal[
            "write",
            "read",
            "mixed",
            "counter",
            "lwt",
            "cdc",
            "mv",
            "si",
            "alternator",
            "user-profile",
        ]
        | None
    ) = Field(
        default=None,
        description="Primary workload type.",
    )
    nemesis_labels: list[str] = Field(
        default_factory=list,
        description="Nemesis (chaos) classes used, e.g. ['SisyphusMonkey', 'ChaosMonkey'].",
    )
    features: list[str] = Field(
        default_factory=list,
        description=(
            "Scylla features specifically tested: encryption-at-rest, tls-ssl, "
            "authorization, cdc, tablets, vnodes, multi-dc, rack-aware, ipv6, kms, etc."
        ),
    )

    @field_validator("supported_backends", mode="before")
    @classmethod
    def validate_backends(cls, v):
        if v is None:
            return v
        for b in v:
            if b not in VALID_BACKENDS:
                raise ValueError(f"Invalid backend '{b}'. Valid: {sorted(VALID_BACKENDS)}")
        return v
