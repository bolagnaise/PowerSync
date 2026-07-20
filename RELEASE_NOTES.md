<!-- release: v2.12.898 -->

## What's Changed

**Stored Flow Power API keys can now be replaced from Configure**
The Flow Power settings page now includes an **Enter or replace Flow Power API key** option. Selecting it opens the secure API-key step even when PowerSync already has a key saved, fixing configurations where the existing credential silently suppressed the only entry page.

**Existing credentials remain private and unchanged unless replaced**
PowerSync never displays or pre-fills the stored secret, preserves it when the replacement option is left disabled, and validates a new key before saving it. Site selection and Flow Power account-metric setup continue normally after replacement.

Update available via HACS
