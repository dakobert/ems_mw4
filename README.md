# EMS MW4

Eigene Home-Assistant-Integration für das Energiemanagement: PV, Hausspeicher, Wärmepumpe, Wallbox und E-Auto, abgestimmt auf PV-Prognose und Tibber-Preise.

Stand: Version 0.3.0. Liest Messwerte, rechnet einen 48-Stunden-Plan für Speicher, Warmwasser und Auto, erfasst die Kosten des Netzbezugs. Der Ausführer ist vorhanden, schreibt aber nur, wenn der Schalter „Steuerung aktiv" eingeschaltet ist (Vorgabe: aus).

- `custom_components/ems_mw4/` – die Integration
