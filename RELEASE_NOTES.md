<!-- release: v2.12.395 -->

## What's Changed

**Fix Profit Max target time parsing**
Smart Optimization now accepts compact Profit Max target times such as `1615` as well as colon-formatted values like `16:15`. This fixes an options-flow mismatch where a configured “full by” time could silently fall back to the default `17:15`, causing Flow Power Profit Max to hold SOC too long and miss an earlier requested target.

Update available via HACS
