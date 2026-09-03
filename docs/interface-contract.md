# Interface Contract

## Governor.get_connection(tenant_id: str)

Returns: a psycopg2 connection object, pooled per tenant (session pooling).

## GET /admin/usage

Returns JSON:

[
  {
    "tenant_id": "string",
    "cpu_percent": 0.0,
    "throttled_ms": 0,
    "timestamp": "ISO8601 string"
  }
]