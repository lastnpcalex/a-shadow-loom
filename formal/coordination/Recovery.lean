import Lean

namespace Recovery

structure State where
  eligible : Bool
  exited : Bool
  stopped : Bool
  used : Bool
  deriving Repr, DecidableEq

def ready (s : State) : Bool := s.eligible && s.exited && !s.stopped && !s.used
def step (s : State) : State × Bool :=
  if ready s then ({s with used := true}, true) else (s, false)

theorem launch_requires_evidence (s : State) (h : (step s).2 = true) :
    s.eligible = true ∧ s.exited = true ∧ s.stopped = false ∧ s.used = false := by
  cases s with
  | mk eligible exited stopped used =>
    cases eligible <;> cases exited <;> cases stopped <;> cases used <;>
      simp_all [step, ready]

theorem never_launch_twice (s : State) : (step (step s).1).2 = false := by
  cases s with
  | mk eligible exited stopped used =>
    cases eligible <;> cases exited <;> cases stopped <;> cases used <;>
      decide

def bools := [false, true]
def states : List State := bools.flatMap fun a => bools.flatMap fun b =>
  bools.flatMap fun c => bools.map fun d => ⟨a,b,c,d⟩

def main (args : List String) : IO Unit := do
  match args with
  | [path] =>
    let rows := states.map fun s => Lean.toJson ([s.eligible, s.exited, s.stopped, s.used], ready s)
    IO.FS.writeFile ⟨path⟩ ((Lean.toJson rows).compress ++ "\n")
  | _ => throw <| IO.userError "usage: recovery_fixtures OUTPUT_PATH"

end Recovery

def main := Recovery.main
