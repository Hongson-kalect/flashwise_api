def arr_to_postgre_json(value):
    """
    Convert Python list/tuple (possibly nested) into
    PostgreSQL array literal format: {1,2,"a",{3,4}}
    """

    if value is None:
        return None

    if not isinstance(value, (list, tuple)):
        # Scalar → escape nếu là string
        if isinstance(value, str):
            escaped = value.replace('"', r'\"')
            return f'"{escaped}"'
        return str(value)

    # Nếu là list/tuple → duyệt đệ quy
    return "{" + ",".join(arr_to_postgre_json(v) for v in value) + "}"
