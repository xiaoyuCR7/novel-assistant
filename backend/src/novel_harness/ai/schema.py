"""Compact schema annotations without modifying property names or JSON literals."""


def compact_schema(schema):
    if not isinstance(schema, dict):
        return schema
    result = {}
    maps = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
    singles = {"items", "additionalProperties", "contains", "not", "if", "then", "else",
               "propertyNames", "unevaluatedProperties", "unevaluatedItems"}
    lists = {"allOf", "anyOf", "oneOf", "prefixItems"}
    for key, value in schema.items():
        if key in {"title", "default"}:
            continue
        if key in maps and isinstance(value, dict):
            result[key] = {name: compact_schema(child) for name, child in value.items()}
        elif key in singles:
            result[key] = compact_schema(value)
        elif key in lists and isinstance(value, list):
            result[key] = [compact_schema(child) for child in value]
        else:
            result[key] = value
    return result
