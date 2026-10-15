namespace LoomCoordination

inductive LaunchView where
  | idle | submitting | running | reconciling | landed | failed
  deriving Repr, DecidableEq, BEq

inductive LaunchEvent where
  | submit | admitted | disconnected | terminal | rejected
  deriving Repr, DecidableEq, BEq

def launchStep (s : LaunchView) (e : LaunchEvent) : LaunchView :=
  match s, e with
  | .idle, .submit => .submitting
  | .submitting, .admitted | .reconciling, .admitted => .running
  | .submitting, .disconnected | .running, .disconnected => .reconciling
  | .idle, _ => .idle
  | _, .terminal => .landed
  | _, .rejected => .failed
  | _, _ => s

def launchVisible : LaunchView → Bool
  | .idle => false
  | _ => true

theorem submission_is_visible : launchVisible (launchStep .idle .submit) = true := by rfl

theorem step_keeps_user_informed (s : LaunchView) (e : LaunchEvent)
    (h : launchVisible s = true) : launchVisible (launchStep s e) = true := by
  cases s <;> cases e <;> simp_all [launchStep, launchVisible]

theorem disconnect_keeps_pending_visible :
    launchStep .submitting .disconnected = .reconciling := by rfl

theorem disconnect_keeps_running_visible :
    launchStep .running .disconnected = .reconciling := by rfl

theorem repeated_submit_cannot_relaunch (s : LaunchView)
    (h : launchVisible s = true) : launchStep s .submit = s := by
  cases s <;> simp_all [launchStep, launchVisible]

theorem arbitrary_trace_keeps_user_informed (events : List LaunchEvent)
    (s : LaunchView) (h : launchVisible s = true) :
    launchVisible (events.foldl launchStep s) = true := by
  induction events generalizing s with
  | nil => exact h
  | cons e rest ih => exact ih _ (step_keeps_user_informed s e h)

end LoomCoordination
