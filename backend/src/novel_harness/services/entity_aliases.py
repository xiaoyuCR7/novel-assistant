"""Shared, literal name matching for Wiki associations and retrieval alias expansion."""

import re

from sqlalchemy import text


def name_matches(content, name):
    if not isinstance(content, str) or not isinstance(name, str):
        return False
    name = name.strip()
    if not name:
        return False
    if len(name) == 1:
        return content.strip().casefold() == name.casefold()
    if name.isascii():
        return bool(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", content, re.I))
    return name.casefold() in content.casefold()


def register_name_matcher(session):
    # Register once per pooled connection; SQLite forbids replacing a function
    # while a statement is active. Filtering here happens before LIMIT,
    # so false substring matches cannot displace eligible sources or alias targets.
    connection = session.connection()
    if not connection.info.get("novel_name_matcher_registered"):
        connection.connection.driver_connection.create_function(
            "novel_name_matches", 2, name_matches, deterministic=True
        )
        connection.info["novel_name_matcher_registered"] = True


def aliases(profile):
    values = profile.get("aliases", []) if isinstance(profile, dict) else []
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))[:20]


def expand_aliases(session, query):
    if not query.strip():
        return query
    register_name_matcher(session)
    names = (
        session.execute(
            text(
                "SELECT DISTINCT e.name FROM entities e, json_each(e.profile, '$.aliases') a "
                "WHERE e.deleted_at IS NULL AND json_type(e.profile,'$.aliases')='array' "
                "AND a.type='text' AND length(trim(a.value)) BETWEEN 1 AND 120 "
                "AND novel_name_matches(:query, a.value) "
                "ORDER BY e.name LIMIT 6"
            ),
            {"query": query},
        )
        .scalars()
        .all()
    )
    return query + (" " + " ".join(names) if names else "")
