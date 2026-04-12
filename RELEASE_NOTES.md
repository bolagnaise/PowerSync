## What's Changed

### Fix: Protobuf version compatibility restored
The `tedapi_combined_pb2.py` file was accidentally regenerated with protobuf 7.x which added a runtime version check incompatible with Home Assistant's protobuf 6.x. This caused PowerSync to fail to load on startup with `VersionError: gencode 7.34.1 runtime 6.32.0`. Reverted to the protobuf 6.x-compatible generated code.

Update available via HACS
