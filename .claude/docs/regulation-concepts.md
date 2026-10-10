# Regulation Engine – Konzepte & Design-Entscheidungen

Dieses Dokument enthält die nicht-offensichtlichen Design-Entscheidungen der Regelungs-Engine.
Lies es bevor du an `regulation.py` oder `parameters.py` arbeitest.

## Feedforward

Kompensiert den Tado-Sensor-Offset (Heizkörper vs. Raum) sofort.
Das ist die Hauptstellgröße – der PI-Anteil korrigiert nur den Restfehler.

## PI-Korrektur

- **Bewusst klein** gehalten: Kp=0.8, Ki=0.003.
- Die PI-Korrektur ist nur für Restfehler zuständig, nicht für die Hauptregelung.
- Größere Werte führen zu Overshoot wegen der Trägheit des Systems (Heizkörper → Raum).

## Integral Deadband (0.3°C)

Das Integral sammelt **nur** wenn der Fehler innerhalb der Deadband liegt (nahe am Ziel).
Verhindert, dass bei großen Abweichungen (z.B. Kaltstart) ein riesiger Integral-Term aufgebaut wird.

## Anti-Windup

Dual-Strategie:
1. **Sättigungs-Block**: Integral wird nicht weiter aufgebaut wenn Output am Limit.
2. **Deadband-Gating mit Decay**: Außerhalb der Deadband wird der bestehende Integral-Term langsam abgebaut.

## Adaptive Gain Scheduling

Automatische Kp-Skalierung abhängig vom Betriebszustand:
- **Kaltstart** (große Abweichung): Multiplikator bis 1.5× für schnelleres Aufheizen.
- **Nahe Ziel**: Multiplikator 1.0× für stabiles Verhalten.
- Interpolation über den vollen Bereich zwischen den Zonenschwellen.
- Schwellen (cold-start/near-target) sind im Options-Flow konfigurierbar.

## Schleifenverstärkung ist (1 + Kp)

Der Feedforward kürzt Tados eigene Messung heraus. Tados interner Regler sieht
`(1 + Kp)·Fehler + Integral + Bremse`. Kp = 0 heißt also nicht „kein P-Anteil".

## D-Bremse (einseitig, Standard: aus)

Tados interner Regler heizt nach negativer Anforderung noch 30–60 min weiter
(eigener Integrator, heißer Heizkörper). Mit P+I kreuzt die Anforderung erst bei
Fehler 0 die Null – immer zu spät. Die Bremse `-(1+Kp)·Td·max(0, Steigung)`
verschiebt den Nulldurchgang um Td·Steigung nach vorn.
- **Nur senkend**, nur bei steigender Raumtemperatur, auf −2 °C begrenzt.
- Steigung per Theil-Sen (robust gegen einzelne Sensor-Ausreißer).
- Aus bei degradiertem Sensor.
**Nicht beidseitig machen** – ein fallender Messwert (Fenster, Ausreißer) darf nie Heizen auslösen.

## Selbstoptimierung (autotune.py)

Siehe `docs/pi-autotune.md`. Kernpunkte:
- SIMC nur für Ti (= 4(τc+θ), τc = 1,5θ) und als Begründung für Td = Nachheizzeit.
- **Kein SIMC-Kc**: Die Verstärkungsschätzung Rate/Anforderung ist selbstbezüglich
  (Anforderung ∝ Kc) und hat Kp in der Simulation bis zur Untergrenze gedrückt.
- Gelerntes nur in Restore-State-Daten, nie in Options (würde Reload auslösen).
- Grenzen, Schrittweiten, Raten, Rollback: `AutotuneConfig` in `parameters.py`.

## Rate Limiting (180s)

Batterieschonung für Tado X TRVs. Die TRVs kommunizieren per Funk und jeder Befehl kostet Batterie.
**Nicht verkürzen** ohne triftigen Grund.
