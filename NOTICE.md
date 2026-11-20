# Sources and provenance

- `src/snnrc`, research configs and their tests derive from
  [himoon8805/Criticality_project_sRC](https://github.com/himoon8805/Criticality_project_sRC),
  snapshot `24fd6cab088e714eba638ebf844be3075c379665`.
  This checkout includes the subsequent blockwise connectivity / reservoir efficiency changes
  and the corrected 8,460-neuron LED configuration used in the local installation work.
- `src/ripples` contains the sensor adapters, persistent statistics, visualization, comparison
  generator and serial integration developed for this project. The transport retains the
  partner handoff's 55-slot model address geometry and legacy helpers; LEDUSB uses the current
  65-slot installation encoder. Historical notes are explicitly separated in docs.
- Installation assets and firmware references were supplied as
  `LED_CURRENT_HANDOFF_2026-11-12.zip`. Hyeol input came from
  `ripples-hyeol-send-sample_20261116.zip`. BLE and microphone formats came from the accompanying handoff documents.
- FastLED-related material retains its upstream license at
  `data_led_example/firmware_reference/FastLED_LICENSE`.
- The original standalone `20260220_model1.py` was removed at the owner's request; the
  maintained reservoir implementation is `src/snnrc`.

No new blanket license is asserted for third-party or supplied material. Attribution and any
existing licenses remain applicable. Generated audio is explicitly synthetic; Hyeol depth
and reservoir spike examples are recorded data. Statistical criticality is not established
by the visual previews alone.
