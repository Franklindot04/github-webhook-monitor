from sqlalchemy import Engine, create_engine


POSTGRESQL_PSYCOPG_SCHEME = "postgresql+psycopg://"


def create_database_engine(
    database_url: str,
    *,
    connect_timeout_seconds: int = 5,
    pool_pre_ping: bool = False,
) -> Engine:
    if not database_url.startswith(POSTGRESQL_PSYCOPG_SCHEME):
        raise ValueError("DATABASE_URL must use postgresql+psycopg")
    if connect_timeout_seconds <= 0:
        raise ValueError("connect_timeout_seconds must be positive")
    return create_engine(
        database_url,
        connect_args={"connect_timeout": connect_timeout_seconds},
        future=True,
        pool_pre_ping=pool_pre_ping,
    )
