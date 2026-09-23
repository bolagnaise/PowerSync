<!-- release: v2.12.1330 -->

## Fixes

### Amber: synthetic forecast tails no longer block solar recharge

When Amber returns fewer intervals than the optimizer's full horizon, PowerSync
must carry a price forward to keep the LP well-formed. Those carried-forward
prices are not provider forecasts, though, and previously a profitable-looking
tail could pin battery charging to zero for the rest of the horizon. The LP
could then hold stored energy for modeled future load even when the solar
forecast showed a later recharge opportunity, suppressing an earlier profitable
battery export.

The optimizer now carries the provider's price-validity mask into its future
value and charge-pinning decisions. Synthetic gaps and tails no longer create
a hard charge prohibition; explicit provider charge blocks, export windows,
reserve floors, and genuine flat tariffs keep their existing behavior.
