<!-- release: v2.12.1791 -->

## Tesla Smart Schedule can retry after a completed Stop

When a Tesla charging session ended through Home Assistant, resolved Bluetooth command history could keep the old connection selected for charging commands and block the next scheduled start. PowerSync now releases that connection for the ended session after an accepted Stop. The command history is preserved, and unresolved commands still block conflicting actions.
