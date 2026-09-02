# GateKeeper
Kernel-level resource isolation for multi-tenant PostgreSQL. Gatekeeper uses Linux cgroups v2 to enforce per-tenant CPU/memory limits on real database backend processes, preventing one tenant's heavy queries from starving others on a shared database.
