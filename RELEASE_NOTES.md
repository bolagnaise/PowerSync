<!-- release: v2.12.1862 -->

## Battery no longer holds charge for price peaks that aren't in the forecast

Dynamic price forecasts such as Amber's only cover about the next day, but PowerSync plans 48 hours ahead. Until now it filled the rest of the plan with the last forecast price. When the forecast ended on an evening peak, every later hour looked peak-priced, including overnight and midday. The battery could then sit idle through a real evening peak while the house imported at peak prices. PowerSync now fills the hours past the forecast with the prices from the same time a day earlier. It covers Amber, Octopus, EPEX, Flow Power and other dynamic-price providers. Update through HACS and restart Home Assistant.
