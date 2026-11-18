"""Opt-in Home Assistant scene discovery; activation is a command, not light state."""

_KNOWN = 'scene_mqtt_known_v1'


def publications(runtime, *, discovery=False):
    engine = getattr(runtime, 'scene_engine', None)
    if engine is None:
        return []
    try:
        status = engine.status()
    except Exception:
        # A damaged scene record pauses scene publication only; device
        # discovery and state continue. Nothing is removed from Home Assistant.
        return []
    scenes = status.get('scenes', [])
    current = {s['id']: s for s in scenes if s.get('exposed') and s.get('status') == 'verified'}
    known = set(runtime.store.get(_KNOWN, []))
    # Existing HA entities remain registered while an edit needs review. A new
    # partial create has never been discovered and must not appear in HA.
    configured = {s['id']: s for s in scenes if s.get('exposed')
                  and (s.get('status') == 'verified' or s['id'] in known)}
    if discovery and not set(current) <= known:
        known.update(current)
        runtime.store.set(_KNOWN, sorted(known))
    # Keep removed scenes durable until the broker acknowledges both retained
    # deletions. Generating or queueing messages is not delivery.
    gone = known - {s['id'] for s in scenes}
    online = (runtime.mesh_ready and runtime.transport.ready and runtime.mqtt_ready
              and runtime.capabilities.get('native_scenes', True)
              and not status.get('operations'))
    messages = []
    for identifier in sorted(known | set(current)):
        uid = f'{runtime.core.node}_scene_{identifier}'
        topic = f'{runtime.core.prefix}/scene/{identifier}'
        scene = configured.get(identifier)
        if identifier in gone:
            # An empty retained payload clears the broker's stored message.
            if discovery: messages.append(_cleanup(runtime, identifier, 'availability', topic + '/availability'))
        else:
            messages.append(runtime.core.intent(topic + '/availability',
                'online' if online and identifier in current and current[identifier].get('activation_ready') is True else 'offline', True))
        if not discovery:
            continue
        payload = ''
        if scene:
            payload = {'name': scene['name'], 'unique_id': uid,
                'command_topic': topic + '/set', 'payload_on': 'ON',
                'availability': [{'topic': runtime.core.prefix + '/availability'},
                    {'topic': runtime.core.prefix + '/mesh/availability'},
                    {'topic': topic + '/availability'}], 'availability_mode': 'all',
                'device': {'identifiers': [uid], 'name': scene['name'],
                    'manufacturer': 'HALO / Avi-on', 'model': 'Native mesh scene'}}
        config_topic = f'homeassistant/scene/{uid}/config'
        messages.append(_cleanup(runtime, identifier, 'config', config_topic) if identifier in gone
                        else runtime.core.intent(config_topic, payload, True))
    return messages


def _cleanup(runtime, identifier, part, topic):
    message = runtime.core.intent(topic, '', True)
    # QoS 1 publish completion means PUBACK, not just a local socket write.
    message.update(qos=1, scene_cleanup={'id': identifier, 'part': part})
    return message


def cleanup_delivered(runtime, message, acknowledged):
    """Count deletions acknowledged in one connection; retain unfinished cleanup durably.

    The caller resets this in-memory map at every connection boundary. A broker
    change or restart must never combine confirmations from different brokers.
    """
    marker = message.get('scene_cleanup')
    if (not isinstance(marker, dict) or marker.get('part') not in ('config', 'availability')
            or message.get('payload') != '' or message.get('qos') != 1 or message.get('retain') is not True):
        return
    identifier = marker.get('id')
    known = set(runtime.store.get(_KNOWN, []))
    if identifier not in known:
        acknowledged.pop(identifier, None)
        return
    try:
        if any(s['id'] == identifier for s in runtime.scene_engine.status()['scenes']):
            acknowledged.pop(identifier, None)
            return
    except Exception:
        return  # Retain cleanup evidence when current scene ownership cannot be read.
    parts = acknowledged.get(identifier, set()) | {marker['part']}
    if parts == {'config', 'availability'}:
        # Commit only after both PUBACKs; a crash before this write just sends
        # the same harmless retained deletions again on the next connection.
        with runtime.store.transaction():
            runtime.store.set(_KNOWN, sorted(known - {identifier}))
        acknowledged.pop(identifier, None)
    else:
        acknowledged[identifier] = parts


def exposed_scene(runtime, identifier):
    engine = getattr(runtime, 'scene_engine', None)
    if engine is None or not runtime.capabilities.get('native_scenes', True):
        return False
    try:
        status = engine.status()
    except Exception:
        return False
    return not status.get('operations') and any(s.get('id') == identifier and s.get('exposed')
        and s.get('status') == 'verified' and s.get('activation_ready') is True for s in status.get('scenes', []))
