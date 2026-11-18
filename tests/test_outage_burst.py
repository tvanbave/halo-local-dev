"""Rapid multi-target commands spanning an outage; synthetic transport only."""
import unittest

import test_mqtt_reliability as mqtt_tests


class OutageBurstTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = mqtt_tests.MqttReliabilityTests.asyncSetUp
    asyncTearDown = mqtt_tests.MqttReliabilityTests.asyncTearDown
    task = mqtt_tests.MqttReliabilityTests.task
    until = mqtt_tests.MqttReliabilityTests.until
    enqueue = mqtt_tests.MqttReliabilityTests.enqueue
    setters = mqtt_tests.MqttReliabilityTests.setters

    async def burst_across(self, boundary):
        await self.runtime.operation.acquire()
        try:
            for level in range(1, 31):
                self.enqueue(level, 40001)
                self.enqueue(level + 30, 40003)
            self.task(self.mqtt.command_loop())
            await self.until(lambda: self.runtime._foreground_requests == 1)
            self.assertEqual(self.setters(), [])
            if boundary == 'broker':
                generation = self.runtime.generation
                self.mqtt._connection(False)
                self.mqtt._connection(True)
                self.assertEqual(self.runtime.generation, generation)
            else:
                self.runtime.session()
            self.assertEqual(self.mqtt.commands, {})
            # These are NEW user commands after recovery, not a replay of the
            # old queue. An old waiting worker must not discard these intents.
            for level in range(100, 112):
                self.enqueue(level, 40001)
                self.enqueue(level + 100, 40003)
        finally:
            self.runtime.operation.release()
        await self.until(lambda: self.mqtt.counts['commands_sent'] == 2)
        self.assertEqual(sorted((p.mcp_address, p.payload[5]) for p in self.setters()),
                         [(40001, 111), (40003, 211)])
        self.assertEqual(self.mqtt.commands, {})

    async def test_broker_recovery_preserves_new_multi_target_burst_without_old_replay(self):
        await self.burst_across('broker')

    async def test_ble_recovery_preserves_new_multi_target_burst_without_old_replay(self):
        await self.burst_across('ble')
