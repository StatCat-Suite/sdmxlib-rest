# HTTP behaviour

## URL grammar

Every route follows SDMX-REST 2.2.2. The grammar decides what a key means:
2.2.2 makes the time dimension a key slot, so a key has one dot-separated
segment per key dimension, time included. On a data structure with
dimensions `FREQ`, `REF_AREA` and `TIME_PERIOD`, the key `A.CA.` has three
segments, and `A.CA` is a 400 that names the dimensions. `all` matches every
key.

SDMX-REST 1.5 and 2.1 keep time out of the key, so their keys are one segment
shorter for the same structure. Read against the 2.2.2 slots, a 1.5 key
shifts every value one dimension and returns a wrong answer without an error.
A consumer that serves the older grammar runs a forwarder that translates
each request before it reaches these routes.

The routes accept two 1.5 spellings: the `all` key, and `startPeriod` and
`endPeriod`. The availability path without a `componentID` is not routed; a
forwarder that serves it maps it to `componentID=all`.

`/availability` needs an explicit version, such as `1.0`; `latest` is a 422.

## Formats

`Accept` chooses the format. Structure routes write:

| Format | Media type |
| --- | --- |
| SDMX-JSON 2.0 | `application/vnd.sdmx.structure+json;version=2.0` |
| SDMX-ML 3.0 | `application/vnd.sdmx.structure+xml;version=3.0` |
| SDMX-ML 2.1 | `application/vnd.sdmx.structure+xml;version=2.1` |

An `Accept` that excludes every format a route writes gets a 406. Common
shorthand, such as `application/json` or a media type without a version,
matches. Responses advertise the two-part version (`version=2.0`) that
SDMX-REST 2.2.2 publishes; requests may send either form.

A structure or metadata query that matches nothing answers 204 for SDMX-JSON;
XML responses keep their own empty form and answer 200. An `/availability`
query that matches nothing answers 204 in every format.

## Languages

SDMX-JSON carries both `name`, in one language, and `names`, in every
language. The service sets `name` and `description` to the best match for
`Accept-Language`, and answers with `Content-Language`. `?lang=fr` overrides
the header, for clients that cannot set one; the service treats it as
`Accept-Language: fr` everywhere, including the ETag.

## Conditional GET

Successful responses carry `ETag`, `Last-Modified` and `Cache-Control`
(`RestSettings.cache_control`). The ETag hashes the app version, the
`effective_token`, the path, the query string, `Accept` and the resolved
language. It does not read the body, so it is computed before the route runs
and is the same with or without compression.

`If-None-Match` and `If-Modified-Since` answer 304 when they match. A new
publication mints a new token, and an annotation change a new fingerprint
([Annotations and overlays](annotations.md)); either changes every ETag.
Error responses carry no ETag and no `Cache-Control`.

## Other behaviour

- **Unknown query parameters are a 400** that names the parameter, so a typo
  such as `?detial=full` fails instead of being ignored.
- **`asOf` is a 501** on the structure and metadata routes. The service
  serves one publication and keeps no history. `/availability` does not take
  `asOf`, so there it is the unknown-parameter 400.
- **`HEAD`** works on every `GET` route and returns the `GET` headers.
- **Compression:** gzip through isa-l, at level 1, for responses of 1 KB or
  more. Parquet, zstd, zip and octet-stream bodies pass uncompressed, as do
the types Starlette never compresses, such as images and event streams.
- **Content no format can carry is a 500** that lists each artefact and the
  sdmxlib rule it breaks, for example a component with no concept.
- **CORS** answers every preflight, including for paths the app does not
  route. It allows `GET`, `HEAD`, `OPTIONS` and `POST`.

## Middleware order

`create_app` installs the middleware in a fixed order. From the outside in:

1. **CORS**, outermost, so a preflight to any path gets an answer. A preflight
   that reached the router would get a 404, which the browser reports as a
   CORS failure.
2. **`outer_middleware`**, the consumer's, which sees the client's request
   and the encoded response.
3. **Performance log**, outside gzip and HEAD, so it counts the bytes sent
   and records the client's method. CORS and `outer_middleware` sit outside
   it, so its time leaves them out.
4. **HEAD**, which turns `HEAD` into `GET` for the layers inside it, inside
   the logging taps so they record the client's method.
5. **gzip**. It compares only the base media type, and the rewrite changes
   only the `version` parameter, so its place beside the rewrite changes no
   response; the test pins it here.
6. **Media-type version rewrite.**
7. **`?lang=`**, outside the conditional-GET layer, so `?lang=fr` hashes like
   `Accept-Language: fr`.
8. **Conditional GET**, innermost.

`tests/test_library_create_app.py` asserts this order.
