"""Planer: 48 Stunden im 15-Minuten-Raster. Reine Funktionen, steuert nichts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .const import SLOT_H, Params
from .forecast import quantile

INF = float("inf")


def _cheapest_window(costs: list[float], candidates: range, length: int) -> int | None:
    best, best_cost = None, INF
    for start in candidates:
        if start + length > len(costs):
            break
        total = sum(costs[start : start + length])
        if total < best_cost - 1e-9:
            best, best_cost = start, total
    return best


def plan_dhw(slots: list[datetime], prices: list[float], net_kw: list[float], dhw_temp: float | None, p: Params) -> list[float]:
    """Eine Warmwasserladung je Kalendertag im günstigsten Fenster. kW je Slot."""
    out = [0.0] * len(slots)
    length = max(1, round(p.dhw_kwh / p.dhw_kw / SLOT_H))
    # Grenzkosten je Slot: PV-Überschuss kostet nur die entgangene Einspeisung
    marginal = []
    for price, net in zip(prices, net_kw):
        surplus = max(0.0, -net)
        share = min(1.0, surplus / p.dhw_kw)
        marginal.append(share * p.feed_in_ct + (1 - share) * price)
    days: dict[Any, list[int]] = {}
    for i, slot in enumerate(slots):
        days.setdefault(slot.date(), []).append(i)
    for n, (_, idx) in enumerate(sorted(days.items())):
        if n == 0 and dhw_temp is not None and dhw_temp >= p.dhw_skip_above_c:
            continue  # heute schon warm genug
        if len(idx) < length:
            continue
        start = _cheapest_window(marginal, range(idx[0], idx[-1] - length + 2), length)
        if start is None:
            continue
        for i in range(start, start + length):
            out[i] = p.dhw_kw
    return out


def plan_car_grid(
    slots: list[datetime], prices: list[float], connected: bool, soc: float | None,
    price_history: list[float], p: Params, pv_surplus_kwh: float = 0.0,
) -> tuple[list[float], dict[str, Any]]:
    """Netzladen des Autos ohne Fahrt: Grundreserve und sehr günstige Preise. kW je Slot."""
    out = [0.0] * len(slots)
    info: dict[str, Any] = {"schwelle_ct": None, "bedarf_kwh": 0.0, "reserve_kwh": 0.0}
    if not connected or soc is None:
        return out, info
    need = max(0.0, (p.car_target_soc - soc) / 100.0 * p.car_kwh)
    info["bedarf_kwh"] = round(need, 1)
    per_slot = p.car_kw * SLOT_H
    remaining = need
    # 1. Grundreserve bis zum nächsten Morgen 7 Uhr, günstigste Slots
    reserve = max(0.0, (p.car_reserve_soc - soc) / 100.0 * p.car_kwh)
    info["reserve_kwh"] = round(reserve, 1)
    if reserve > 0:
        deadline = next((i for i, s in enumerate(slots) if i > 0 and s.hour == 7 and s.minute == 0), len(slots))
        order = sorted(range(deadline), key=lambda i: prices[i])
        for i in order:
            if reserve <= 0:
                break
            energy = min(per_slot, reserve)
            out[i] = energy / SLOT_H
            reserve -= energy
            remaining -= energy
    # 2. sehr günstig: unterstes Quantil und deutlich unter dem Mittel
    history = [v for v in price_history if v is not None] or list(prices)
    q = quantile(history, p.car_cheap_quantile)
    mean = sum(history) / len(history)
    threshold = min(q, mean - p.car_cheap_below_mean_ct) if q is not None else None
    info["schwelle_ct"] = None if threshold is None else round(threshold, 2)
    # Deckt der erwartete PV-Überschuss den Bedarf, wird nicht aus dem Netz geladen
    info["pv_ueberschuss_kwh"] = round(pv_surplus_kwh, 1)
    info["pv_deckt_bedarf"] = pv_surplus_kwh >= remaining > 0
    if threshold is not None and remaining > 0 and not info["pv_deckt_bedarf"]:
        for i in sorted(range(len(slots)), key=lambda i: prices[i]):
            if remaining <= 0 or prices[i] > threshold:
                break
            if out[i] > 0:
                continue
            energy = min(per_slot, remaining)
            out[i] = energy / SLOT_H
            remaining -= energy
    return out, info


def plan_battery(prices: list[float], net_kw: list[float], soc_pct: float, p: Params) -> dict[str, list[float]]:
    """Speicherfahrplan per dynamischer Programmierung über den Ladestand.

    net_kw: Verbrauch minus PV je Slot (positiv = Bedarf). Kosten in ct.
    Entladen deckt nur Hausbedarf, nie Einspeisung. Laden aus PV oder Netz.
    """
    n = len(prices)
    step = p.battery_step_kwh
    e_min = p.battery_kwh * p.battery_min_soc / 100.0
    levels = int((p.battery_kwh - e_min) / step + 1e-9)
    high_level = int((p.battery_kwh * 0.8 - e_min) / step)
    start = min(levels, max(0, int(round((p.battery_kwh * soc_pct / 100.0 - e_min) / step))))
    terminal = 0.98 * ((quantile(prices, 0.25) or 0.0) * p.battery_eff_discharge - p.battery_wear_ct)

    value = [-(terminal * step * lvl) for lvl in range(levels + 1)]  # Restwert als negative Kosten
    choice: list[list[int]] = []
    for t in range(n - 1, -1, -1):
        price, net = prices[t], net_kw[t] * SLOT_H  # kWh Bedarf (+) oder Überschuss (-)
        new_value = [INF] * (levels + 1)
        new_choice = [0] * (levels + 1)
        for lvl in range(levels + 1):
            max_kw = p.battery_charge_kw if lvl < high_level else p.battery_charge_kw_high
            max_up = min(levels - lvl, int(max_kw * SLOT_H * p.battery_eff_charge / step))
            options = {0}
            if net < 0:
                options.add(min(max_up, int(-net * p.battery_eff_charge / step)))  # nur PV-Überschuss
            options.add(max_up)  # volle Leistung, ggf. aus dem Netz
            options.add(max_up // 2)
            if net > 0:
                need = min(net, p.battery_discharge_kw * SLOT_H) / p.battery_eff_discharge
                options.add(-min(lvl, int(need / step)))  # Hausbedarf decken
            best, best_d = INF, 0
            for d in sorted(options):  # bei Gleichstand: früher entladen, weniger laden
                if d >= 0:
                    grid = net + d * step / p.battery_eff_charge
                    wear = 0.0
                else:
                    grid = net + d * step * p.battery_eff_discharge
                    wear = -d * step * p.battery_wear_ct
                cost = (grid * price if grid > 0 else grid * p.feed_in_ct) + wear + value[lvl + d]
                cost -= 1e-4 * abs(d) * (n - t) / n  # bei Gleichstand früher handeln
                if cost < best - 1e-7:
                    best, best_d = cost, d
            new_value[lvl], new_choice[lvl] = best, best_d
        value = new_value
        choice.append(new_choice)
    choice.reverse()

    soc: list[float] = []
    batt_kw: list[float] = []
    grid_kw: list[float] = []
    lvl = start
    for t in range(n):
        d = choice[t][lvl]
        net = net_kw[t] * SLOT_H
        if d >= 0:
            bus = d * step / p.battery_eff_charge
        else:
            bus = d * step * p.battery_eff_discharge
        lvl += d
        soc.append(round((e_min + lvl * step) / p.battery_kwh * 100.0, 1))
        batt_kw.append(round(-bus / SLOT_H, 3) + 0.0)  # positiv = Entladen, wie Kostal
        grid_kw.append(round((net + bus) / SLOT_H, 3) + 0.0)
    return {"soc": soc, "battery_kw": batt_kw, "grid_kw": grid_kw}


def battery_action(batt_kw: float, net_before_kw: float, soc: float, min_soc: float) -> str:
    """Klartext für den Speicher im Slot."""
    if batt_kw < -0.05:
        surplus = max(0.0, -net_before_kw)
        return "Netzladen" if -batt_kw > surplus + 0.05 else "PV-Laden"
    if batt_kw > 0.05:
        return "Entladen"
    if net_before_kw > 0.05 and soc > min_soc + 1.0:
        return "Halten"
    return "Leerlauf"


def _shift(heat: list[float], modes: list[str], start: int, end: int, mode: str, p: Params) -> bool:
    """Heizenergie aus [start, end) in die Slots davor verlagern. False, wenn davor kein Platz ist."""
    pre_len = max(1, round(p.heat_preheat_h / SLOT_H))
    pre = [i for i in range(max(0, start - pre_len), start) if modes[i] == "normal"]
    energy = sum(heat[start:end])
    if energy > 0 and not pre:
        return False
    for i in range(start, end):
        heat[i], modes[i] = 0.0, mode
    for i in pre:
        heat[i] += energy * p.heat_preheat_loss / len(pre)
        modes[i] = "vorheizen"
    return True


def plan_heating(
    slots: list[datetime], prices: list[float], temps: list[float | None], heat_kw: list[float],
    room_temp: float | None, quiet: tuple[int, int] | None, p: Params,
) -> tuple[list[float], list[str], list[dict[str, Any]]]:
    """Sperren in Preisspitzen und einmaliges Ruhefenster. Vor jeder Sperre wird vorgeheizt.

    Rückgabe: (Heizleistung je Slot, Modus je Slot, geplante Sperren).
    Modi: normal, vorheizen, sperre, ruhe.
    """
    n = len(slots)
    heat = list(heat_kw)
    modes = ["normal"] * n
    blocks: list[dict[str, Any]] = []
    abort = p.room_target_c - p.room_band_down_c
    if quiet is not None:
        start, end = max(0, quiet[0]), min(n, quiet[1])
        if end > start:
            _shift(heat, modes, start, end, "ruhe", p)
    if not p.heat_block_enabled:
        return heat, modes, blocks
    max_len = max(1, round(p.heat_block_max_h / SLOT_H))
    gap = round(p.heat_block_gap_h / SLOT_H)
    pre_len = max(1, round(p.heat_preheat_h / SLOT_H))
    frost_after = round(p.heat_block_frost_after_h / SLOT_H)
    for _ in range(8):
        best: tuple[float, int, int, float] | None = None
        for start in range(pre_len, n):
            for length in range(4, max_len + 1):
                end = start + length
                if end > n:
                    break
                if any(m != "normal" for m in modes[start - pre_len : end]):
                    continue
                if any(b["start_index"] - gap < end and start < b["end_index"] + gap for b in blocks):
                    continue
                look = temps[start : min(n, end + frost_after)]
                if any(t is None or t <= p.heat_block_frost_c for t in look):
                    continue
                energy = sum(heat[start:end]) * SLOT_H
                if energy <= 0:
                    continue
                price_block = sum(prices[start:end]) / length
                price_pre = sum(prices[start - pre_len : start]) / pre_len
                advantage = price_block - price_pre * p.heat_preheat_loss
                if price_block - price_pre < p.heat_block_min_adv_ct:
                    continue
                if start <= pre_len and (room_temp is None or room_temp < abort + 0.3):
                    continue  # steht unmittelbar bevor und der Raum ist schon knapp
                saving = energy * advantage
                if best is None or saving > best[0]:
                    best = (saving, start, end, price_block - price_pre)
        if best is None or best[0] <= 0:
            break
        _, start, end, adv = best
        if not _shift(heat, modes, start, end, "sperre", p):
            break
        blocks.append({"start_index": start, "end_index": end, "start": slots[start].isoformat(timespec="minutes"),
                       "ende": slots[end - 1].isoformat(timespec="minutes"), "preisvorteil_ct": round(adv, 1),
                       "ersparnis_ct": round(best[0], 1)})
    blocks.sort(key=lambda b: b["start_index"])
    return heat, modes, blocks


def build_plan(
    slots: list[datetime], prices: list[float], estimated: list[bool], pv_kw: list[float],
    base_kw: list[float], heat_kw: list[float], soc_pct: float, dhw_temp: float | None,
    car_connected: bool, car_soc: float | None, price_history: list[float], p: Params,
    temps: list[float | None] | None = None, room_temp: float | None = None,
    quiet: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Gesamtplan für Heizung (Sperren, Ruhefenster), Warmwasser, Auto und Speicher."""
    n = len(slots)
    heat_forecast = list(heat_kw)
    heat_kw, heat_mode, heat_blocks = plan_heating(
        slots, prices, temps if temps is not None else [None] * n, heat_kw, room_temp, quiet, p
    )
    fixed = [base_kw[i] + heat_kw[i] - pv_kw[i] for i in range(n)]
    dhw = plan_dhw(slots, prices, fixed, dhw_temp, p)
    # Für das Auto nutzbarer PV-Überschuss: vorsichtig gerechnet, nach Auffüllen des Speichers
    raw_surplus = sum(max(0.0, -(fixed[i] + dhw[i])) for i in range(n)) * SLOT_H
    battery_fill = max(0.0, (100.0 - soc_pct) / 100.0 * p.battery_kwh) / p.battery_eff_charge
    pv_for_car = max(0.0, raw_surplus * p.pv_safety - battery_fill)
    car_grid, car_info = plan_car_grid(slots, prices, car_connected, car_soc, price_history, p, pv_for_car)
    net = [fixed[i] + dhw[i] + car_grid[i] for i in range(n)]
    batt = plan_battery(prices, net, soc_pct, p)

    # Auto aus PV-Überschuss. Reihenfolge: Speicher bis 50 %, dann Auto, dann Speicher voll.
    car_pv = [0.0] * n
    remaining = car_info["bedarf_kwh"] - sum(car_grid) * SLOT_H if car_connected else 0.0
    if remaining > 0:
        first = next((i for i in range(n) if batt["soc"][i] >= p.car_after_battery_soc), None)
        if soc_pct >= p.car_after_battery_soc:
            first = 0
        if first is not None:
            for i in range(first, n):
                if remaining <= 0:
                    break
                surplus = -net[i]
                if surplus >= p.car_min_kw and car_grid[i] == 0:
                    power = min(surplus, p.car_kw, remaining / SLOT_H)
                    car_pv[i] = round(power, 3)
                    remaining -= power * SLOT_H
            if any(car_pv):
                net = [net[i] + car_pv[i] for i in range(n)]
                batt = plan_battery(prices, net, soc_pct, p)
    grid = list(batt["grid_kw"])

    actions = [battery_action(batt["battery_kw"][i], net[i], batt["soc"][i], p.battery_min_soc) for i in range(n)]
    cost_by_day: dict[str, float] = {}
    kwh_by_day: dict[str, float] = {}
    for i, slot in enumerate(slots):
        key = slot.date().isoformat()
        imp = max(0.0, grid[i]) * SLOT_H
        cost_by_day[key] = cost_by_day.get(key, 0.0) + imp * prices[i] / 100.0
        kwh_by_day[key] = kwh_by_day.get(key, 0.0) + imp
    return {
        "slots": slots,
        "price_ct": prices,
        "price_estimated": estimated,
        "pv_kw": pv_kw,
        "base_kw": base_kw,
        "heat_kw": [round(v, 3) for v in heat_kw],
        "heat_forecast_kw": heat_forecast,
        "heat_mode": heat_mode,
        "heat_blocks": heat_blocks,
        "dhw_kw": dhw,
        "car_kw": [round(car_grid[i] + car_pv[i], 3) for i in range(n)],
        "car_grid_kw": car_grid,
        "battery_kw": batt["battery_kw"],
        "soc": batt["soc"],
        "grid_kw": grid,
        "battery_action": actions,
        "cost_eur": {k: round(v, 2) for k, v in cost_by_day.items()},
        "import_kwh": {k: round(v, 2) for k, v in kwh_by_day.items()},
        "car": car_info,
    }


def reason(plan: dict[str, Any], i: int) -> str:
    """Begründung der Speicheraktion im Slot i in einem Satz."""
    action = plan["battery_action"][i]
    price = plan["price_ct"][i]
    later = plan["price_ct"][i + 1 : i + 97]
    peak = max(later) if later else price
    if action == "Netzladen":
        return f"Preis jetzt {price:.1f} ct, später bis {peak:.1f} ct. Laden aus dem Netz lohnt trotz Verlusten."
    if action == "PV-Laden":
        return "PV-Überschuss geht in den Speicher."
    if action == "Entladen":
        return f"Preis jetzt {price:.1f} ct. Der Speicher deckt den Hausbedarf."
    if action == "Halten":
        return f"Speicher wird für teurere Stunden aufgespart (bis {peak:.1f} ct), jetzt Netzbezug zu {price:.1f} ct."
    if plan["soc"][i] <= 6.0 and plan["grid_kw"][i] > 0.05:
        return "Speicher leer, Bezug aus dem Netz."
    return "Kein Bedarf, keine Aktion."


def next_change(values: list[Any], i: int) -> int | None:
    """Index des nächsten Slots mit anderem Wert."""
    for j in range(i + 1, len(values)):
        if values[j] != values[i]:
            return j
    return None


def next_start(values: list[float], i: int) -> int | None:
    """Index des nächsten Slots ab i, in dem der Wert größer null ist."""
    for j in range(i, len(values)):
        if values[j] > 0:
            return j
    return None
