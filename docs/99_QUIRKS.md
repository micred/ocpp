Known quirks
============

## JuiceBox charging profile kind

The tested JuiceBox OCPP 1.6 implementation supports effective charging current
control with `chargingProfileKind = Absolute` only. `Relative` charging profiles
can be accepted by the charger but were observed not to reduce the actual line
current.

The OCPP integration has a generic per-charge-point **Charge rate profile kind**
option for generated charge-rate profiles. The default should remain `Relative`
for existing chargers, while JuiceBox users should configure this option as
`Absolute`. This keeps the workaround opt-in and avoids hard-coding a
JuiceBox-specific behavior path in the integration code.
