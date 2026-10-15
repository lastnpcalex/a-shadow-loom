import Lean

namespace AgentAccess

def conflict (same leftWrite rightWrite : Bool) : Bool :=
  same && (leftWrite || rightWrite)

theorem readers_coexist (same : Bool) : conflict same false false = false := by
  cases same <;> rfl

theorem independent_writers_coexist (same : Bool) : conflict same false false = false := by
  cases same <;> rfl

theorem conflicting_access_blocks (same left right : Bool)
    (h : same = true) (w : left = true ∨ right = true) : conflict same left right = true := by
  cases same <;> cases left <;> cases right <;> simp_all [conflict]

theorem conflict_symmetric (same left right : Bool) :
    conflict same left right = conflict same right left := by
  cases same <;> cases left <;> cases right <;> rfl

def disclose (sensitive human released : Bool) : Bool := !sensitive || human || released

theorem private_unreleased (h r : Bool) (hh : h = false) (hr : r = false) :
    disclose true h r = false := by
  simp [disclose, hh, hr]

def admitted (conflict capacity localCapacity : Bool) : Bool :=
  !conflict && capacity && localCapacity

theorem admission_requires_no_conflict (a b c : Bool) (h : admitted a b c = true) : a = false := by
  cases a <;> cases b <;> cases c <;> simp_all [admitted]

def inherits (parentSensitive childSensitive isLocal readNarrow writeNarrow : Bool) : Bool :=
  readNarrow && writeNarrow && (!parentSensitive || (childSensitive && isLocal))

theorem sensitive_inherits_local (c l r w : Bool) (h : inherits true c l r w = true) :
    c = true ∧ l = true := by
  cases c <;> cases l <;> cases r <;> cases w <;> simp_all [inherits]

end AgentAccess

def bits : List Bool := [false, true]
def row (flags : List Bool) (value : Bool) : Lean.Json :=
  Lean.Json.arr #[Lean.toJson flags, Lean.toJson value]

def main (args : List String) : IO Unit := do
  let triples := bits.flatMap fun a => bits.flatMap fun b => bits.map fun c => [a,b,c]
  let five := bits.flatMap fun a => bits.flatMap fun b => bits.flatMap fun c =>
    bits.flatMap fun d => bits.map fun e => [a,b,c,d,e]
  let oracle := Lean.Json.mkObj [
    ("conflicts", Lean.toJson (triples.map fun f => row f (AgentAccess.conflict f[0]! f[1]! f[2]!))),
    ("disclosure", Lean.toJson (triples.map fun f => row f (AgentAccess.disclose f[0]! f[1]! f[2]!))),
    ("admission", Lean.toJson (triples.map fun f => row f (AgentAccess.admitted f[0]! f[1]! f[2]!))),
    ("inheritance", Lean.toJson (five.map fun f => row f (AgentAccess.inherits f[0]! f[1]! f[2]! f[3]! f[4]!)))]
  IO.FS.writeFile (args.headD "cases.json") (oracle.pretty ++ "\n")
