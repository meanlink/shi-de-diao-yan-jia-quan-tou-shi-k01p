from sqlalchemy import BigInteger, Boolean, Double, Float, Integer, JSON, String, Text, inspect, text

from src.config.database import Base, engine
from src import models  # noqa: F401 — registers every table on Base.metadata


def _sql_type(kind, dialect: str) -> str:
    if isinstance(kind, Boolean):
        base = 'BOOLEAN'
    elif isinstance(kind, (Double, Float)):
        base = 'DOUBLE PRECISION'
    elif isinstance(kind, BigInteger):
        base = 'BIGINT'
    elif isinstance(kind, Integer):
        base = 'INTEGER'
    elif isinstance(kind, JSON):
        base = 'JSON'
    elif isinstance(kind, Text):
        base = 'TEXT'
    elif isinstance(kind, String):
        base = f'VARCHAR({getattr(kind, "length", None) or 255})'
    else:
        base = 'TEXT'
    if dialect == 'sqlite':
        return {'DOUBLE PRECISION': 'FLOAT', 'BOOLEAN': 'BOOLEAN', 'JSON': 'JSON'}.get(base, base)
    return base


def _add_missing_columns():
    """create_all() never alters existing tables; add new nullable columns in place."""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    dialect = engine.dialect.name
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            present = {column['name'] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present or column.primary_key:
                    continue
                if column.foreign_keys:
                    # Avoid dialect-specific constraint syntax; the FK is enforced by the app
                    # layer and the column carries the same scalar type.
                    kind = _sql_type(list(column.foreign_keys)[0].column.type, dialect)
                else:
                    kind = _sql_type(column.type, dialect)
                if dialect == 'sqlite' and isinstance(column.type, Boolean):
                    kind += ' DEFAULT 0'
                connection.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {kind}'))


def migrate():
    Base.metadata.create_all(engine)
    _add_missing_columns()
    _seed_ledger_clock()


def _seed_ledger_clock():
    """Pre-create the single clock row so first concurrent writers never race an INSERT."""
    from sqlalchemy.dialects import postgresql, sqlite
    dialect = engine.dialect.name
    with engine.begin() as connection:
        if dialect == 'sqlite':
            connection.execute(sqlite.insert(models.LedgerState).values(id=1, version=0).prefix_with('OR IGNORE'))
        elif dialect == 'postgresql':
            connection.execute(postgresql.insert(models.LedgerState).values(id=1, version=0).on_conflict_do_nothing(
                index_elements=[models.LedgerState.id]))
        else:  # pragma: no cover - only sqlite/postgres are used by this project
            from sqlalchemy import text
            connection.execute(text('INSERT INTO ledger_state (id, version) SELECT 1, 0 '
                                    'WHERE NOT EXISTS (SELECT 1 FROM ledger_state WHERE id = 1)'))


if __name__ == '__main__':
    migrate()
