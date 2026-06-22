from __future__ import annotations

from sleepagent.services.postgres import initialize_postgres_schema


def main() -> None:
    initialize_postgres_schema()
    print("SleepAgent PostgreSQL schema is initialized.")


if __name__ == "__main__":
    main()

