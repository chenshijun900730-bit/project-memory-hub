from project_memory_hub.discovery.fingerprint import (
    fingerprint_git_remote,
    fingerprint_manifests,
    normalize_git_remote,
)
from project_memory_hub.discovery.policy import DiscoveryPolicy
from project_memory_hub.discovery.scanner import ProjectScanner

__all__ = (
    "DiscoveryPolicy",
    "ProjectScanner",
    "fingerprint_git_remote",
    "fingerprint_manifests",
    "normalize_git_remote",
)
