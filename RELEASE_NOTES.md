<!-- release: v2.12.619 -->

## What's Changed

**Allow grid charging stays off during negative prices**
Smart Optimization now treats the Allow grid charging toggle as a hard constraint even when EPEX or other import prices go zero or negative. Negative-price slots can still be shown in the plan, but PowerSync will no longer convert them into forced battery grid-charge commands when grid charging is disabled.

**Custom TOU settings open the custom tariff form**
Selecting Other / Custom TOU from the pricing options now opens the custom tariff form directly instead of first showing the GloBird / AEMO settings page. This makes fixed import/export tariffs easier to configure for users outside the Australian GloBird and AEMO VPP flows.

**Sungrow idle mode preserves solar charging**
Sungrow idle and no-discharge holds now use a discharge-rate cap instead of Forced+Stop, so the battery can still accept charge while discharge is blocked. PowerSync restores the previous discharge limit afterward and falls back to a near-zero 10 W cap on firmware that rejects an exact zero limit.

**Tesla PowerSync proxy helpers use the correct API base**
EV schedule reserve and export-rule helpers now use the PowerSync proxy API base when the PowerSync Tesla provider is selected, instead of falling back to the direct Fleet API base. This keeps backup-reserve and export-rule reads/writes on the same authenticated proxy path as the rest of the PowerSync Tesla integration.

Update available via HACS
