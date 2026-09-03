"""
Temporary stub Governor used by backend/frontend development
before the real cgroup-based Governor is implemented.
Interface must match docs/interface-contract.md exactly.
"""


class StubGovernor:
    def get_connection(self, tenant_id: str):
        # TODO: replace with real pooled + cgroup-assigned connection
        raise NotImplementedError(
            "Stub governor — real implementation pending"
        )