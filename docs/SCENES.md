# Native scenes

Open **Scenes** in the bridge to create a preset for one or several colour lights.
Choose lights individually or add them with a group shortcut. To give several lights the same preset, choose their brightness and white temperature and select **Apply to selected lights**. You can still adjust any light individually before saving. Save sends colour then brightness as one preset for each light, then waits for matching readbacks of both settings before showing Verified.
The scene's actions let you edit, activate, verify or remove it.

**Add to Home Assistant** optionally exposes a scene entity over MQTT. Unsharing
removes discovery without deleting the native preset. Shared scenes needing review
remain unavailable until their saved operation is resolved.

## Interrupted saves

If a save stops, use **Read-only recovery**. It checks what was stored without
sending programming commands. If more work is needed, **Continue save** is a
separate action after verification. When only the first part was attempted, continuation sends the untouched second part. If both parts were attempted, a full-preset retry is offered only after fresh reads show values from the saved operation and you explicitly request it. Nothing retries on
restart or automatically after a timeout. Incomplete scenes cannot activate.

## When a light does not answer

Reading a stored setting changes nothing, so a lost reply is asked again, up to
three times. Programming commands are still sent once only. A light that stays
silent is named on the page.

If that light is simply switched off and the change does not write to it, choose
**Carry on without** it. Nothing is sent to that light. The scene then shows
**Not checked on** that light until **Verify saved settings** passes with it
powered. Until then the bridge has not confirmed that light is clear of the
scene's slot: if it holds a preset there from another controller, it will also
respond when the scene is activated.

## Leaving an unfinished operation

An unfinished save or removal blocks pairing, removal and other changes until it
is resolved. If it cannot be finished, **Set aside** closes it without sending
anything and keeps the record in Operations. When a command may have reached a
light, the bridge reserves that scene slot and does not use it again. A scene that
already existed stays in the list and cannot be activated until **Verify saved
settings** passes.

**Forget scene** removes a scene from the bridge and from Home Assistant without
contacting the lights, and reserves its slot. Use **Remove scene** instead whenever
the lights can be reached, because only removal clears their stored settings.
Reserved slots count against the twenty the bridge uses and are not released
automatically.

Set `"native_scenes": false` under `capabilities` in the configuration to switch
scene features off.

## Native behavior and qualification

Verified activation sends one native scene broadcast, regardless of member count.
Saved verification survives bridge restarts and ordinary connection changes.
Activation checks the saved proof without rereading every light. Inventory or
external scene changes can invalidate that proof; use **Verify saved settings**
when requested before activating again. Background status waits yield to controls
once any active transmission finishes. Submission does not prove that every lamp
visibly changed.

Scene activation from Off can still pause before the lights change. The same
normal command showed that behavior in HALO Home, while changes from already-on
were prompt in that comparison. An optional captured-keypad command comparison
showed no consistent speed improvement, so normal bridge and MQTT activation keep
the HALO app format. The exact cause of the remaining pause is unconfirmed.

A19 references 1 and 2 have physical create/edit/recall/delete isolation evidence.
RL4 single-light recovery, two-light creation/editing and removal/isolation have verified readbacks and user-confirmed operation. Single-light scene discovery and activation through Home Assistant also passed a physical check. RL4 and other compatible colour lights remain marked experimental while restart and lamp power-cycle persistence checks continue. Reading a light again after it loses power, and activating a scene with a light left unchecked, are covered by offline tests only. Maximum capacity remains unmeasured.
Each complete preset and each removal checks both stored brightness and colour properties. Empty reads after a colour-only attempt do not rule out internal staging; that reference stays reserved for recovery.

The supported reference range is 1–20, an implementation limit rather than measured
hardware capacity. Scene assignment to keypad buttons is a separate future feature.
Captured status values remain raw; acknowledgements alone never complete a save.

Home Assistant uses its [MQTT scene integration](https://www.home-assistant.io/integrations/scene.mqtt/).
