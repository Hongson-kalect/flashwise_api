def get_deep(data, keys, default=None):
    """
    Hàm đào sâu vào dict giống Optional Chaining của JS
    Sử dụng: get_deep(s_data, ['usage', language_code, 'value'])
    """
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
        else:
            return default
    return data if data is not None else default