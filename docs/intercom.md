# Intercom

Real-time voice conversation between two EchoMuse devices — the Drop-In
feature EchoMuse does not provide today. The full design is in the
project's design notes; this file is the protocol as built, phase by
phase, so a port or a dashboard can implement against it without reading
the code.

## Shape

Intercom is a mode on the device, not a relay through the controller and
not a Home Assistant feature. The controller is the switchboard: it knows
both devices, sets up the call, and fans hangup out to both ends. Call
audio flows device to device and never crosses the controller or Home
Assistant — Home Assistant's `assist_satellite` API has no intercom
concept.

Calls start from Home Assistant, not from the dashboard: a sentence
("drop in on lounge") or an automation calls the calling Echo's
`drop_in` service, and "hang up" — spoken, or the action button — ends
it (Phase 4). The dashboard API underneath still exists, but it is the
fallback, not the front door.

Sendspin is the precedent for this shape: a device-resident mode the
controller switches, with its own port and its own session state.

## Phase 1 — the mode switch (done)

Phase 1 carries no audio. What it proves is that the switch itself is
solid: a call can be started and ended by the controller, both devices
enter and leave the mode together, the in-call ring shows, and nothing
gets stuck — no wedged speaker, no lost wake word, no call that survives
its switchboard. (Phase 1's front door was the dashboard API; Phase 4
moves it to Home Assistant — the switch underneath is unchanged.)

- The device announces the `intercom` capability at registration.
- The controller offers a call to both devices with `intercom_offer`
  (`session`, `peer_id`, `peer_label`, `token`, `role`, `peer_addr`).
  Each device enters the attempt — the callee rings, the caller waits —
  and answers with `intercom_state` (`ringing` / `calling`).
- The controller ends a call with `intercom_end` (`session`, `reason`),
  fanned out to both devices. Each device flushes the ring, answers
  `intercom_state` (`idle`), and returns to normal satellite mode.
- Teardown is idempotent on both sides: ending an ended session is a
  no-op, and a duplicated or late end can never wedge the device.
- A device that loses its controller link ends the call locally and
  clears the ring — a call cannot survive the loss of its switchboard.
- The wake-word scorer stays live for the whole call: intercom mode
  never sets the flag that pauses wake scoring for a voice turn. A wake
  during a call is detected and reported exactly as a wake during
  playback is; the controller declines to start a turn for it until
  Phase 3 nests turns inside the call.

## Phase 2 — peer audio (done)

Phase 2 streams the call. The peer link is a plain TCP connection on
port 8929 (Sendspin holds 8928), carrying raw 48 kHz mono S16LE after
a short handshake; the firewall rule for the port opens only while a
call is being attempted or is live. This version was walkie-talkie
grade by design — the voice plane's ~1 s prime dominated latency — and
what it proved was the lifecycle plus the AEC question: whether echo
cancellation holds under continuous two-way speech on real hardware.

- **Ringing and consent.** The callee rings (slow green pulse) for up
  to 30 s; a Dot press answers — the device dials the caller itself,
  and the press is consumed on the device rather than forwarded, so it
  cannot also start a voice turn. A Dot press on the caller while it
  waits cancels the attempt. Ring-and-press is the default. A ring that
  times out ends the attempt for both ends. Auto-answer is a per-Echo
  opt-in (Phase 4): with **Allow drop-in** on, the callee answers
  itself with a chime and a fast green pulse, and its microphone opens
  without its own wake word — see docs/listening.md for the exception
  that writes into the listening rules.
- **Handshake and auth.** The dialler opens with `EMIC`, version 1,
  the session id, the controller-issued token, and its own device id;
  the listener accepts only the session it was offered and only the
  token that came with it, and answers with one accept byte. A stray
  connection — or a device from another call — cannot inject audio
  into a call it was not invited to. The token is a secret: it is
  never logged, and the handshake is the only place it travels after
  the offer.
- **Audio in.** The call taps the DataClient's post-AEC mono mic
  frames, the same mono STT hears. Tapping after AEC is the whole
  point: the canceller is one shared, stateful Speex instance fed by
  the speaker's echo reference, so a second consumer calling Process
  on the raw mic would double-feed its echo state. The tap is a hook
  on the mic goroutine — it must not block, and it copies the frame.
- **Resampling.** A streaming linear-interpolation upsampler, 16 kHz
  to 48 kHz (3x), holds the last sample of each frame so the
  interpolation across frame boundaries stays continuous. It is the
  cheapest resampler that is not wrong; a better filter (or a codec,
  Phase 4) replaces the file, not its callers.
- **Audio out.** Peer PCM goes straight into the voice plane
  (`PumpPeriod`), which paces the receiver to playback speed — a fast
  peer is throttled by TCP backpressure, not by a growing buffer.
- **Endings.** A peer that closes, a dial that fails, a ring that
  times out, or a speaker error all end the call locally with a
  reason (`peer_disconnected`, `dial_failed`, `no_answer`,
  `speaker_error`); the device reports `intercom_state` (`idle`) and
  the controller ends the session for both ends. A speaker-only device
  (no mic) still takes the call — it hears the peer, it just sends
  nothing back.

### Device state

One device, one state at a time (`device/internal/intercom`):

| State | Meaning |
| --- | --- |
| `idle` | Normal satellite mode, no call |
| `ringing` | Offered as callee; ringing, waiting for the button |
| `calling` | Offered as caller; waiting for the callee to answer |
| `in_call` | In a call, audio flowing |
| `suspended` | In a call, this end running a nested Home Assistant turn (Phase 3) |

Transitions: `idle → ringing` (callee) or `idle → calling` (caller) on
offer (re-offering the same session is a no-op); `ringing` / `calling →
in_call` when the peer link comes up; `in_call → suspended → in_call`
around a nested turn; anything `→ idle` on end. Ending when idle is a
no-op. The ring is a slow green pulse while the call is being
attempted, solid green once it is flowing. An auto-answered attempt
(Allow drop-in on) rings with a **fast** green pulse (500 ms) instead
of the slow one, so the room can tell the callee answered itself —
the pulse is the visible half of the chime's notice.

### Controller session

`controller/em_intercom.py` holds the session: an id, the two device
ids, a one-time token, and `active` / `ended`. A device is in at most
one call at a time. The token is a session secret — it is never logged —
and the callee checks a dialler against it on the peer link. An idle
report for a session that is still active ends the session: a device
only reports idle mid-call because it left the call on its own — ring
timeout, peer gone, dial failed, button cancel — and the other end must
hear about it through `intercom_end`, not be left ringing.

Starting a call requires both devices to be connected,
intercom-capable, unmuted, not in a voice turn and not already in a
call; anything else is a 409, never a half-started call. If offering to
one device fails after the other was offered, the session is ended
immediately so no device is left in a call the controller does not
know about.

## Phase 3 — conversational latency and voice control (done)

Phase 3 makes the call conversational. Three changes, all in the
audio path and the turn:

- **Low-prime voice plane.** While a call is flowing, the device
  lowers the voice plane's prime gate from 24 periods (~1 s) to 3
  (~130 ms) — `SetIntercomMode` on the speaker, called by the Call
  on connect and restored on suspend and teardown. The gate only
  matters at stream start, so switching mid-call takes effect on the
  next stream: a nested turn's TTS reply gets the full prime, and
  the resumed call gets the low one. The music plane is untouched.
- **Jitter buffer.** The receiver holds ~170 ms of peer audio (4
  periods) before the first pump, then releases it a period at a
  time; a peer sending faster than realtime cannot grow the latency
  without limit — past 8 periods the oldest audio is dropped. With
  the low prime, a call now starts ~300 ms after the first byte.
  While suspended the receiver keeps reading and discarding, so the
  peer's sender never blocks on its write deadline, and the cushion
  is dropped so no stale audio pumps on resume.
- **Nested HA turns.** A wake during a flowing call starts a normal
  HA turn, nested inside it: `_run_voice_locked` suspends the call
  first (the mic stops going to the peer, peer audio stops playing,
  the voice plane is flushed and its prime restored) and resumes it
  in the turn's finally, so a turn that dies mid-command cannot leave
  a call suspended. The suspend is not awaited for an ack — it lands
  in milliseconds, and the command does not start for a beat after
  the wake word. A wake while the Dot is ringing, calling, or already
  suspended is still declined: the attempt owns the device until it
  resolves, and a wake during a suspend belongs to the turn already
  running. The far end hears silence during the turn — no hold chime
  or LED cue on that side yet.
- **Voice hangup.** "End the call" (and "hang up", "hangup", "end
  call", "end the intercom call", with an optional "please" and
  trailing punctuation) is caught at STT_END: the turn is cancelled
  with `abort_ha` so HA's pipeline stops before the intent runs,
  then the call is ended for both ends. The STT_END → intent window
  is tight and HA may occasionally start an intent first, which is
  harmless by default. The hook is wired per turn on the current
  satellite (a reconnect makes a new one); it only acts when the
  device is in a call, so ordinary turns are unaffected. A substring
  match is deliberately not used — "don't end the call" must not
  hang up.

### Voice and button behaviour during a call (Phases 1–3)

- **Wake word:** during a flowing call it starts a nested HA turn
  (above) — every ordinary HA command works mid-call, including
  "end the call". During a call attempt (ringing / calling) or an
  already-suspended call it is detected and logged, no turn starts:
  the private listener closes the session (`listen_close`, reason
  `intercom`); the controller-scored listener drops the wake before
  arming capture routing. Either way the call continues.
- **Action button:** a tap during a flowing call hangs up (the
  controller ends the session for both ends). A tap while ringing
  answers; a tap while calling cancels. Button hangup is the one
  hangup that works while muted, because the button event flows over
  the control plane whatever the mic state is.
- **Mute:** a muted device never calls and never auto-answers. Mute is
  device-sovereign: the controller cannot unmute a device, and a muted
  device cannot be called.

## Phase 4 — Home Assistant first (done)

Phase 4 moves the front door to Home Assistant. Nothing in the call
itself changes — the switchboard lifecycle, teardown on link loss, the
per-call token and the post-AEC peer audio are exactly as Phases 1–3
built them. What changes is who may start and end a call, and how the
callee answers.

- **Services, not a custom integration.** Each Echo exposes two
  ESPHome user-defined services, gated on the `intercom` capability,
  so a Home Assistant sentence automation can call them with no custom
  integration: `drop_in` takes one `target` (the peer device id) and
  `hang_up` takes nothing. `drop_in` fails if the target is missing,
  offline, or the caller itself; `hang_up` with no call is a
  successful no-op, so an automation can call it unconditionally.
  The services are listed to HA only on intercom-capable firmware,
  and the controller answers each call with success or an error
  message HA can log.
- **A sentence automation.** One automation per calling Echo (or an
  area slot — HA has no intercom concept, so there is no built-in
  "which Echo am I" in the sentence):

  ```yaml
  automation:
    - alias: "Drop in on the lounge"
      trigger:
        - platform: conversation
          command: "drop in on lounge"
      action:
        - service: esphome.kitchen_drop_in
          data:
            target: lounge
  ```

  The service name is the calling Echo's — `esphome.<device>_drop_in` —
  and `target` is the callee's device id. Saying "hang up" still goes
  through the Phase 3 voice intercept (STT_END, whole-transcript
  match): the `hang_up` service is the automation path, for an
  automation that ends a call, or a dashboard button, without speaking.
- **Allow drop-in.** A per-Echo setting (`allowDropIn`, off by
  default, in the dashboard's Intercom stage, gated on the capability)
  that auto-answers: the callee plays a chime and rings with a fast
  green pulse, then its microphone opens without its own wake word.
  The chime is the callee's only audible notice that its mic went
  live, so it plays even when the wake sound is off — the level still
  respects the setting. Off, the callee rings and waits for a press,
  exactly as Phase 2.
- **Reporting.** The callee reports the call in `listen_state` as
  `in_call` for as long as it lasts (docs/listening.md), so the
  dashboard shows the microphone is live to the peer rather than
  keeping showing private listening. The report is the state the
  device is in; it changes nothing about mic routing.

### API

- `POST /api/devices/{id}/intercom/start` — body `{peer_id}`. Returns
  the session. Admin only.
- `POST /api/devices/{id}/intercom/end` — body `{session_id?}`; `{}`
  ends whatever call the device is in. Admin only.
- `GET /api/devices` surfaces `intercomCapable`, `intercom` (the
  device's last reported state), `intercomSession` and `intercomPeer`.

The API is the fallback now, not the front door: the services above
are how calls are meant to start and end.

## What is not built yet

- **Phase 4 — codec and transport, only if needed:** FLAC first for
  bandwidth, Opus on a datagram transport for loss resilience — only if
  Phase 2/3 measurements show raw PCM glitching on real devices.
- **Peer hold cue:** during a nested turn the far end hears silence
  and sees nothing different — no chime, no LED change. Worth adding
  (a controller-to-peer `intercom_hold` message, or an in-band tone)
  once the nested turn itself is proven on hardware.
- **Announcements during a call:** an announcement (or a timer alarm)
  writes to the same voice plane as peer audio, so the two interleave
  for the length of the announcement. Suppression or queueing during
  calls is not built yet.
- **Dashboard call UI:** calls start and end from Home Assistant or
  the API; the dashboard has no call button or in-call view. The
  Intercom config stage (Allow drop-in) is the only dashboard surface.
