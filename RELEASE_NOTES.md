<!-- release: v2.12.1667 -->

## SAJ H2 power limits with Modbus 3.1.3

Fixed battery charge and discharge power limits being read incorrectly after updating SAJ H2 Inverter Modbus to 3.1.3. PowerSync now understands its new raw power-setting values while continuing to support the percentage sensors from older versions, including low and zero limits.

Update PowerSync through HACS and restart Home Assistant. PowerSync needs no manual scaling changes; personal automations or dashboards that read the affected SAJ sensors directly still need the adjustments described by the SAJ Modbus release. TOU slot power inputs remain percentages.
