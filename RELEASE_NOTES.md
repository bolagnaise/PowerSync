<!-- release: v2.12.1874 -->

## Away cars no longer hold battery for charging tonight

An away car only counts in tonight's battery plan from when PowerSync expects it home. Before, a car hours away kept its full Smart Schedule or Tesla schedule in the plan, so PowerSync held battery and import for charging that couldn't happen.

PowerSync now plans an away car's charging from, in order: your answer, the car's navigation arrival time when it's heading home, or its usual return time when **Use historical EV data** is on. When the car can't reach home on its range it charges on the way, so nothing is planned. Otherwise nothing is planned until it arrives, and live data takes over once it's home. If you rely on evening charging being planned while you're out, turn on **Use historical EV data**. This only changes the battery plan; it never starts or stops charging.

## Tell PowerSync when a car is coming home

The Home Assistant EV panel and the mobile app's EV charging tab have a new **Away cars** section showing each away car and how it's treated tonight, with **Not home tonight** and **Back to automatic**. Automations can use the new `power_sync.set_ev_return` action. When it's unclear whether a car is coming home and the answer would change tonight's plan by at least that car's amount (default $1.00, set per car with a slider), the mobile app asks with **Before**, **After** and **Not tonight** buttons. See [Away cars](https://github.com/bolagnaise/PowerSync/wiki/EV-Smart-Charging#away-cars).

Update through HACS and restart Home Assistant. The notification buttons need mobile app build 503 or later.
