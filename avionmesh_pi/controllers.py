"""Model-specific observation and verified keypad mapping, no gesture inference."""
from .protocol import decode_controller


def binding_status(table, slot):
    """One verified-session slot's HA meaning; never infer a physical gesture."""
    if isinstance(table, dict) and table.get('mapping_evidence') == 'cached_table' and slot not in table.get('cached_slots', []):
        return 'needs_refresh', None
    rows = table.get("slots", []) if isinstance(table, dict) else []
    row = next((r for r in rows if r.get("slot") == slot), None)
    if row is None:
        return "needs_refresh", None
    target, flags, level = row.get("target"), row.get("flags"), row.get("level")
    if target == 65535 and flags == 0 and level == 0:
        return "unassigned", row
    if (type(target) is not int or not 0 < target < 65535 or flags != 8
            or type(level) is not int or not 0 < level <= 255):
        return "unsupported_binding", row
    if sum(r.get("target") == target for r in rows) != 1:
        return "ambiguous_target", row
    return "cached" if table.get("mapping_evidence") == "cached_table" else "ready", row


class ControllerRouter:
    def __init__(self):
        self.local_modes = set()
        self.baselines = {}
        self.native_targets = {}
        self.native_baselines = {}
        self.zero_queries = {}
        self.bindings = {}
        self.battery_cache = None

    def reset(self):
        self.baselines.clear()
        self.native_targets.clear()
        self.native_baselines.clear()
        self.zero_queries.clear()
        self.bindings.clear()
        if self.battery_cache is not None:
            self.battery_cache.reset()

    def mapping_for_events(self, target):
        """Readback bindings and stale routing evidence remain separate stores."""
        return self.bindings.get(target) or (self.battery_cache.mapping(target)
                                            if self.battery_cache is not None else None)

    def observe_query(self, target, transaction, now):
        if transaction == 0:
            self.zero_queries[target] = now

    def decode(self, packet, product, now):
        p = packet.payload
        if (product == 91 and packet.crypto_source in self.local_modes and packet.opcode == 115
                and len(p) == 4 and p[:2] == b"\x01\x0a"):
            source, sequence, level, transaction = packet.crypto_source, packet.sequence, p[3], p[2]
            previous = self.baselines.get(source)
            query = transaction != 0 or now - self.zero_queries.get(source, -10000) < 2000
            if previous:
                old_sequence, old_level, old_time = previous
                advance = (sequence - old_sequence) & 0xffffff
                if not advance or advance >= 0x800000:
                    if advance >= 0x800000 and now - old_time > 10000:
                        self.baselines[source] = (sequence, level, now)
                    return None
            self.baselines[source] = (sequence, level, now)
            if previous is None or query:
                return None
            event = {"event_type": "off" if not level else "on" if level == 255 else "brightness",
                     "source": source, "sequence": sequence, "brightness": level,
                     "opcode": 115, "payload": p.hex(), "input_mode": "local_level_reports"}
            if 0 < advance <= 128 and now - old_time <= 15000 and level and old_level and level != old_level:
                event["direction"] = "raise" if level > old_level else "lower"
                event["delta"] = level - old_level
            return event
        event = decode_controller(packet, product)
        if event and product in (81, 91) and "brightness" in event:
            source, level = packet.crypto_source, event["brightness"]
            previous = self.native_baselines.get(source)
            target = event.get("target")
            if previous and (self.native_targets.get(source) != target or now - previous[2] > 10000):
                previous = None
            if previous:
                old_sequence, old_level, old_time = previous
                advance = (packet.sequence - old_sequence) & 0xffffff
                if not advance or advance >= 0x800000:
                    if advance >= 0x800000 and now - old_time > 10000:
                        self.native_baselines[source] = (packet.sequence, level, now)
                    return None
                if advance <= 128 and 0 <= now - old_time <= 1500 and 0 < level < 255 and old_level and level != old_level:
                    event["direction"] = "raise" if level > old_level else "lower"
                    event["delta"] = level - old_level
            self.native_baselines[source] = (packet.sequence, level, now)
            self.native_targets[source] = target
        if event and product in (126, 127):
            table = self.bindings.get(packet.crypto_source)
            if table is None and product == 126 and self.battery_cache is not None:
                table = self.battery_cache.for_event(packet, event)
            if table:
                matches = [row for slot in range(1, 5)
                           for status, row in [binding_status(table, slot)]
                           if status in ("ready", "cached") and row["target"] == event.get("target")]
                if len(matches) == 1:
                    event["button"] = matches[0]["slot"]
                    event["button_mapping"] = table.get("mapping_evidence", "verified_table")
                    if event.get("brightness") == matches[0]["level"]:
                        event["activation"] = True
        return event


def named_inputs(event, product=None):
    result = []
    if event.get("activation") and "button" in event:
        result.append(("button_" + str(event["button"]), "activated"))
    kind = event["event_type"]
    if kind in ("on", "off", "raise", "lower"):
        result.append((kind, "command"))
    # Slider events include both endpoints. Keypad presets remain only commands
    # and verified activations; they must not masquerade as dimmer movements.
    if "brightness" in event and (product in (81, 91) or product is None and kind == "brightness"):
        result.append(("brightness", "command"))
    if event.get("direction") in ("raise", "lower") and event["direction"] != kind:
        result.append((event["direction"], "command"))
    return result
