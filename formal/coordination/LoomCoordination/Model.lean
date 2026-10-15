namespace LoomCoordination

inductive Outcome where
  | pending | completed | failed | cancelled | interrupted
  deriving Repr, DecidableEq, BEq

inductive Queue where
  | waiting | paused | dispatched
  deriving Repr, DecidableEq, BEq

structure State where
  outcome : Outcome := .pending
  exited : Bool := false
  leased : Bool := true
  queue : Queue := .waiting
  authorized : Bool := false
  diagnostic : Bool := false
  deriving Repr, DecidableEq, BEq

inductive Event where
  | toolData (text : String)
  | diagnosticError
  | terminal (outcome : Outcome)
  | stop (interrupt : Bool)
  | processExited
  | release
  | resumeQueue
  | dispatch
  deriving Repr, DecidableEq, BEq

inductive Effect where
  | none | releaseLease | launchQueued | pauseQueue
  deriving Repr, DecidableEq, BEq

def terminal (s : State) (outcome : Outcome) : State :=
  if s.outcome ≠ .pending ∨ outcome = .pending then s
  else { s with outcome := outcome,
                queue := if outcome = .completed then s.queue else
                  if s.queue = .dispatched then .dispatched else .paused }

def step (s : State) : Event → State × Effect
  | .toolData _ => (s, .none)
  | .diagnosticError => ({s with diagnostic := true}, .none)
  | .terminal outcome => (terminal s outcome, .none)
  | .stop interrupt => (terminal s (if interrupt then .interrupted else .cancelled), .none)
  | .processExited => ({terminal s .failed with exited := true}, .none)
  | .release =>
      if s.exited = true ∧ s.leased = true ∧ s.outcome ≠ .pending then
        ({s with leased := false}, .releaseLease)
      else (s, .none)
  | .resumeQueue =>
      if s.queue = .paused then ({s with queue := .waiting, authorized := true}, .none)
      else (s, .none)
  | .dispatch =>
      if s.leased = true ∨ s.exited = false ∨ s.queue ≠ .waiting then (s, .none)
      else if s.outcome = .completed ∨ s.authorized = true then
        ({s with queue := .dispatched}, .launchQueued)
      else ({s with queue := .paused}, .pauseQueue)

def failed (s : State) : Bool :=
  s.outcome == .failed || (s.outcome == .pending && s.diagnostic)

def run (s : State) (events : List Event) : State :=
  events.foldl (fun current event => (step current event).1) s

-- Text is genuinely arbitrary, not one of a few fixture error strings.
theorem tool_text_cannot_change_state (s : State) (a b : String) :
    step s (.toolData a) = step s (.toolData b) := by rfl

theorem tool_text_is_inert (s : State) (text : String) :
    step s (.toolData text) = (s, .none) := by rfl

theorem completion_overrides_diagnostics (diagnostic : Bool) :
    failed (step {diagnostic := diagnostic} (.terminal .completed)).1 = false := by
  cases diagnostic <;> rfl

theorem release_requires_exit (s : State) (notExited : s.exited = false) :
    (step s .release).2 = .none := by simp [step, notExited]

theorem dispatch_requires_released_lane (s : State) (leased : s.leased = true) :
    (step s .dispatch).2 = .none := by simp [step, leased]

theorem dispatch_requires_exit (s : State) (notExited : s.exited = false) :
    (step s .dispatch).2 = .none := by simp [step, notExited]

theorem paused_queue_needs_resume (s : State) (paused : s.queue = .paused) :
    step s .dispatch = (s, .none) := by simp [step, paused]

theorem unsuccessful_unapproved_never_launches (s : State)
    (unsuccessful : s.outcome ≠ .completed) (unapproved : s.authorized = false) :
    (step s .dispatch).2 ≠ .launchQueued := by
  simp only [step]
  split <;> simp_all

theorem dispatch_is_at_most_once (s : State) :
    (step (step s .dispatch).1 .dispatch).2 ≠ .launchQueued := by
  simp only [step]
  split
  · rename_i h
    simp
  · split <;> simp

def safe (s : State) : Prop := s.leased = false → s.exited = true

theorem terminal_preserves_safe (s : State) (o : Outcome) (h : safe s) :
    safe (terminal s o) := by
  unfold terminal
  split <;> simpa [safe] using h

theorem step_preserves_safe (s : State) (e : Event) (h : safe s) : safe (step s e).1 := by
  cases e with
  | toolData _ => exact h
  | diagnosticError => simpa [step, safe] using h
  | terminal o => exact terminal_preserves_safe s o h
  | stop i => exact terminal_preserves_safe s _ h
  | processExited => simp [step, safe]
  | release =>
    simp only [step]
    split <;> simp_all [safe]
  | resumeQueue =>
    simp only [step]
    split <;> simpa [safe] using h
  | dispatch =>
    simp only [step]
    split
    · exact h
    · split <;> simpa [safe] using h

theorem trace_preserves_safe (events : List Event) (s : State) (h : safe s) :
    safe (run s events) := by
  induction events generalizing s with
  | nil => exact h
  | cons e rest ih => exact ih _ (step_preserves_safe s e h)

-- Browser evidence order. Identity matching is supplied by exact run/message
-- routing; no text, timestamp or selected-branch guess supplies that evidence.
structure View where
  landed : Bool
  deriving Repr, DecidableEq, BEq

def viewStep (s : View) (sameIdentity incomingLanded : Bool) : View × Bool :=
  if !sameIdentity || (s.landed && !incomingLanded) then (s, false)
  else ({landed := incomingLanded}, true)

theorem foreign_view_inert (s : View) (incoming : Bool) :
    viewStep s false incoming = (s, false) := by rfl

theorem landed_never_regresses (s : View) (same incoming : Bool) (h : s.landed = true) :
    (viewStep s same incoming).1.landed = true := by
  cases s; cases same <;> cases incoming <;> simp_all [viewStep]

theorem stale_running_snapshot_rejected : viewStep ⟨true⟩ true false = (⟨true⟩, false) := by rfl

end LoomCoordination
