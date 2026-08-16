from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

def normalize(raw):
    url = raw
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)

    parts = urlsplit(url)
    filtered_query = [
        (k, v)
        for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in {"sslmode", "channel_binding"}
    ]
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urlencode(filtered_query), parts.fragment
    ))

sample = (
    "postgresql://user:p%40ss@ep-test.neon.tech/neondb"
    "?sslmode=require&channel_binding=require&application_name=mifinanzas"
)
result = normalize(sample)

assert result.startswith("postgresql+asyncpg://")
assert "sslmode=" not in result
assert "channel_binding=" not in result
assert "application_name=mifinanzas" in result
assert "p%40ss" in result
print("NEON URL NORMALIZATION: OK")
