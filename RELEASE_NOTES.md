<!-- release: v2.12.1506 -->

### GloBird ZeroHero daily credit

Estimated Net Cost Today and daily/monthly average costs now include your configured ZeroHero daily credit after a fully observed qualifying window ends. A $1 credit reduces an estimated net cost of $0.39 to −$0.61. The dashboard shows the credit as **unverified** until GloBird returns complete next-day API cost data; that data confirms or corrects the amount without counting it twice.

Update through HACS and restart Home Assistant. Missing readings or starting after the window begins leave eligibility unknown, with no local credit deducted. API confirmation requires connected GloBird portal credentials for the same site and currently supports accounts returning a single service. Other conditional credits, demand charges and bill adjustments remain excluded from the estimate.
