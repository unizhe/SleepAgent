# Technical debt

- Several environment variables and development diagnostic URLs retain `RADAR_AGENT` in their externally configured names. They are compatibility-sensitive deployment contracts, not a runtime namespace.
- `sleep_domain/authority_migration.py` and `legacy_migration.py` remain because live Perceptor authority cutover still consumes their typed import contracts. Remove them only after deployed data has completed cutover.
- Some test names and frozen audit prose retain historical phase terminology. They are evidence labels, not production organization.
- PostgreSQL integration proofs require an available Docker/PostgreSQL runtime; unit and SQLite proofs do not substitute for that deployment check.
