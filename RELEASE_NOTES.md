<!-- release: v2.12.1821 -->

## Correct import costs outside a custom tariff's allowance window

For a custom tariff with a daily import allowance, PowerSync now records grid import outside the allowance window at your plan's own rate for that time. Previously all import was costed at the allowance window's rate, so daily import cost and savings were understated when you imported outside the window, for example at the evening peak. Import inside the window is costed as before.
