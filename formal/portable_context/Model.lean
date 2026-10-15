import Lean

/- Specification only: production does not yet consume this decision model.
   Fits means the COMPLETE rendered request plus reserves fits its destination.
   Evidence truth, tokenization and summary fidelity are outside these proofs. -/
namespace PortableContext

inductive Preparation where
  | untried | succeeded | failed | unsupported
  deriving Repr, DecidableEq

inductive Action where
  | resume | replay | reuse | prepare | execute | retrieve | stop
  deriving Repr, DecidableEq

structure State where
  stopped : Bool
  compatibleNative : Bool
  replayFits : Bool
  validViewFits : Bool
  preparation : Preparation
  resultFits : Bool
  deriving Repr, DecidableEq

def decide (s : State) : Action :=
  if s.stopped then .stop
  else if s.compatibleNative then .resume
  else if s.replayFits then .replay
  else if s.validViewFits then .reuse
  else match s.preparation with
    | .untried => .prepare
    | .succeeded => if s.resultFits then .execute else .prepare
    | .failed | .unsupported => .retrieve

theorem native_fast_path (s : State) (h : s.stopped = false)
    (n : s.compatibleNative = true) : decide s = .resume := by
  simp [decide, h, n]

theorem stop_dominates (s : State) (h : s.stopped = true) : decide s = .stop := by
  simp [decide, h]

theorem retrieval_requires_failure (s : State) (h : decide s = .retrieve) :
    s.preparation = .failed ∨ s.preparation = .unsupported := by
  cases s with
  | mk stopped native replay view prep fits =>
    cases stopped <;> cases native <;> cases replay <;> cases view <;>
      cases prep <;> cases fits <;> simp_all [decide]

theorem execution_requires_fit (s : State) (h : decide s = .execute) :
    s.preparation = .succeeded ∧ s.resultFits = true := by
  cases s with
  | mk stopped native replay view prep fits =>
    cases stopped <;> cases native <;> cases replay <;> cases view <;>
      cases prep <;> cases fits <;> simp_all [decide]

/- Indexed fragments support splitting one giant record. Accepted progress
   must start at exactly the previously accepted offset and advance within it. -/
def advance (done total start stop : Nat) (cancelled : Bool) : Nat :=
  if cancelled = false ∧ start = done ∧ done < stop ∧ stop ≤ total
  then stop else done

theorem cancelled_does_not_advance (d t a b : Nat) : advance d t a b true = d := by
  simp [advance]

theorem accepted_fragment_contiguous (d t a b : Nat)
    (h : advance d t a b false ≠ d) : a = d ∧ d < b ∧ b ≤ t := by
  unfold advance at h
  split at h
  next good => exact good.2
  next => contradiction

/- Prefix validity uses exact records here, rather than assuming hashes cannot
   collide. Production stores canonical hashes and must check ancestry too. -/
def validPrefix (covered branch : List Nat) : Prop :=
  covered ≠ [] ∧ branch.take covered.length = covered

theorem exact_coverage (covered branch : List Nat) (h : validPrefix covered branch) :
    covered ++ branch.drop covered.length = branch := by
  have split := List.take_append_drop covered.length branch
  rw [h.2] at split
  exact split

def admits (capacity history instructions tools media output margin : Nat) : Prop :=
  history + instructions + tools + media + output + margin ≤ capacity

theorem admission_includes_output (c h i t m o s : Nat)
    (fit : admits c h i t m o s) : h + o ≤ c := by
  unfold admits at fit
  omega

def prepName : Preparation → String
  | .untried => "untried" | .succeeded => "succeeded"
  | .failed => "failed" | .unsupported => "unsupported"
def actionName : Action → String
  | .resume => "resume" | .replay => "replay" | .reuse => "reuse"
  | .prepare => "prepare" | .execute => "execute" | .retrieve => "retrieve"
  | .stop => "stop"

def states : List State :=
  [false, true].flatMap fun stopped =>
  [false, true].flatMap fun native =>
  [false, true].flatMap fun replay =>
  [false, true].flatMap fun view =>
  [Preparation.untried, .succeeded, .failed, .unsupported].flatMap fun prep =>
  [false, true].map fun fits => ⟨stopped, native, replay, view, prep, fits⟩

def main (args : List String) : IO Unit := do
  match args with
  | [path] =>
    let rows := states.map fun s => Lean.Json.mkObj [
      ("stopped", Lean.toJson s.stopped), ("native", Lean.toJson s.compatibleNative),
      ("replay_fits", Lean.toJson s.replayFits), ("view_fits", Lean.toJson s.validViewFits),
      ("preparation", Lean.toJson (prepName s.preparation)),
      ("result_fits", Lean.toJson s.resultFits), ("action", Lean.toJson (actionName (decide s)))]
    IO.FS.writeFile ⟨path⟩ ((Lean.toJson rows).compress ++ "\n")
  | _ => throw <| IO.userError "usage: lean --run Model.lean cases.json"

end PortableContext

def main := PortableContext.main
