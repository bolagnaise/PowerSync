<!-- release: v2.12.1845 -->

## Smart Schedule no longer loses the car to Solar Surplus

With Solar Charging also on for a car, standalone Solar Surplus could take the car at 0 A while Smart Schedule was retrying a start, for example in a free-power window while the home battery was using all the solar. Each time Smart Schedule took the car back it had to send a Stop, a sleeping Tesla couldn't confirm it, and charging commands then paused until PowerSync could check the result, so the car could miss the whole window. Solar Surplus now leaves a car alone while its Smart Schedule has decided to charge it, and can use the car again once Smart Schedule no longer wants to charge it.

## EV start failures now say why

When a Tesla start failed, the EV status sometimes said only "could not pre-set the safe initial rate to 16A" with nothing after it. It now gives the reason, such as no spare site import capacity, a charging command still awaiting confirmation, or another PowerSync control holding the car. Charging decisions are unchanged.

Update through HACS and restart Home Assistant.
