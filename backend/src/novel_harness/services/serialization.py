"""Dependency-free SQLAlchemy record serialization."""

from sqlalchemy.inspection import inspect


def serialize(model: object) -> dict:
    mapper = inspect(model).mapper
    return {column.key: getattr(model, column.key) for column in mapper.column_attrs}
