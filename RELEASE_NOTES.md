<!-- release: v2.12.919 -->

## Fixed

- Wake Tesla Powerwall dispatch reliably after force charge, force discharge,
  self-consumption, and restore transitions by briefly changing backup reserve
  before restoring the exact configured hardware reserve. The transition is
  serialized with user reserve commands and remains safe across cleanup retries
  and Home Assistant restarts.
- Treat Tesla grid-charging automation writes as successful when every gateway
  accepts the command but omits the readback field. Direct/manual controls
  remain strict and still require confirmed readback.
- Recognize legacy Tesla config entries in Grid Export and Preserve Charge
  automation actions instead of incorrectly skipping them as non-Tesla systems.
