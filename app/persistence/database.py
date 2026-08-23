from sqlalchemy import Engine, create_engine


POSTGRESQL_PSYCOPG_SCHEME = "postgresql+psycopg://"


def create_database_engine(database_url: str) -> Engine:
    if not database_url.startswith(POSTGRESQL_PSYCOPG_SCHEME):
        raise ValueError("DATABASE_URL must use postgresql+psycopg")
    return create_engine(database_url, future=True)
