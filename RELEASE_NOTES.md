<!-- release: v2.12.1216 -->

## What's Changed

**Powerwall local control now shares Tesla protocol schemas safely**
PowerSync now uses the published `tesla-protocol` package for TEDAPI message
schemas instead of registering its own copy of `tedapi_combined.proto`. This
prevents the duplicate protobuf descriptor collision that could occur when
PowerSync and integrations using `aiopowerwall`, including Teslemetry, were
loaded in the same Home Assistant process.

**Powerwall networking diagnostics remain credential-free**
The schema migration preserves PowerSync's allowlisted networking-status
output. Wi-Fi configuration fields such as the SSID and password are not
returned by the diagnostic helper.

Update available via HACS
