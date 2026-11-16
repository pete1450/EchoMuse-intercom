package listen

// State values, reported to the controller as listen_state. The DataClient
// holds the same strings as its ListenStream/ListenLocal/ListenDegraded.
//
// StateInCall is NOT one of the wake-stream states Resolve returns, and
// must never be passed to SetListenState: it is only a REPORT, sent while
// a call flows, saying the microphone is live to the peer. The wake
// stream underneath is still local (the scorer keeps running), so the
// resolved state is unchanged — the report is overridden at the point
// it is sent, and restored when the call ends.
const (
	StateStream   = "stream"
	StateLocal    = "local"
	StateDegraded = "degraded"
	StateInCall   = "in_call"
)

// Resolve decides what the device does with its wake stream.
//
// A device with no microphone (micErr says why) is DEGRADED whatever was
// asked: it can neither stream nor listen, and reporting either would be a
// statement about listening that is not true.
//
// Private listening needs all three: the operator asked for on-device wake
// word (owwOnDevice=on), the controller understands sessions, and a scorer is
// loaded. The first two missing mean STREAM, which is what the device did
// before this existed and what an older controller depends on. The third
// missing means DEGRADED, never stream: a device asked to listen privately
// that cannot score must not quietly start sending its microphone instead.
// The button still works, and scorerErr says why to whoever looks.
func Resolve(micErr string, onDevice bool, controllerHasSessions bool, scorerLoaded bool,
	scorerErr string) (state, reason string) {
	if micErr != "" {
		return StateDegraded, micErr
	}
	if !onDevice || !controllerHasSessions {
		return StateStream, ""
	}
	if !scorerLoaded {
		if scorerErr == "" {
			scorerErr = "wake word runtime not loaded"
		}
		return StateDegraded, scorerErr
	}
	return StateLocal, ""
}
