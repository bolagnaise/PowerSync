<!-- release: v2.12.650 -->

## What's Changed

**Finer force power increments**
Force charge and force discharge power controls now support 50 W increments instead of 500 W steps. This gives ZeroHERO and other low-export automations much finer control when they only need a small export target to avoid importing.

**Restart force-mode state hardening**
PowerSync now reads restart restore state through a guarded entry lookup before reporting force-mode status. This avoids a setup-time edge case where force-mode state could be queried before the entry data was fully available.

Update available via HACS
