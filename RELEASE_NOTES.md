<!-- release: v2.12.849 -->

## Fixes

- Fixed SolaX Gen4/Gen5/Gen6 minimum SOC writes being forced to 15% when the upstream Home Assistant number entity supports a lower reserve such as 10%. PowerSync now honors the entity's advertised range while retaining the 15% fallback for legacy entities without range metadata.
