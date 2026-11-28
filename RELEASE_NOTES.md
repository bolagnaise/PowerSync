<!-- release: v2.12.1875 -->

## Old coming-home answers no longer change tonight's plan

A coming-home notification tapped long after it was sent could still change tonight's plan. For example, tapping yesterday's **Not tonight** marked the car away until the next morning and dropped tonight's charging.

Each question now expires at 10:00 the next morning. An answer is applied only to the newest question for the car's current trip, once, and not after the car has arrived home. Answers from the Home Assistant EV panel, the app's **Away cars** card and automations are unchanged.

Update through HACS and restart Home Assistant. The notification buttons need mobile app build 503 or later.
