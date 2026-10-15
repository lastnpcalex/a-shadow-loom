import Lean

namespace Search

inductive Policy where
  | excluded | cloud | sensitive
  deriving DecidableEq, Repr

def mayRead (active assigned sameSource isLocal : Bool) (policy : Policy) : Bool :=
  active && assigned && sameSource && (policy != .excluded) && (policy == .cloud || isLocal)

theorem read_requires_scope (a b c d : Bool) (p : Policy)
    (h : mayRead a b c d p = true) : a = true ∧ b = true ∧ c = true := by
  cases a <;> cases b <;> cases c <;> cases d <;> cases p <;> simp_all [mayRead]

theorem excluded_never_read (a b c d : Bool) : mayRead a b c d .excluded = false := by
  simp [mayRead]

theorem sensitive_requires_local (a b c d : Bool)
    (h : mayRead a b c d .sensitive = true) : d = true := by
  cases a <;> cases b <;> cases c <;> cases d <;> simp_all [mayRead]

def ancestor (conv thread workspace parent unseen : Bool) : Bool :=
  conv && thread && workspace && parent && unseen

theorem ancestry_requires_exact_identity (a b c d e : Bool)
    (h : ancestor a b c d e = true) :
    a = true ∧ b = true ∧ c = true ∧ d = true ∧ e = true := by
  cases a <;> cases b <;> cases c <;> cases d <;> cases e <;> simp_all [ancestor]

def tools : List String := ["search_conversations", "read_conversation_context", "get_conversation_details"]
def toolAllowed (name : String) : Bool := tools.contains name

theorem tools_only (name : String) (h : toolAllowed name = true) :
    name = "search_conversations" ∨ name = "read_conversation_context" ∨ name = "get_conversation_details" := by
  simpa [toolAllowed, tools, List.contains_iff_mem, List.mem_cons, List.mem_singleton] using h

def route (nonempty classified excluded sensitive : Bool) : Nat :=
  if !nonempty || !classified || excluded then 0 else if sensitive then 1 else 2

theorem cloud_requires_no_sensitive (a b c d : Bool) (h : route a b c d = 2) :
    a = true ∧ b = true ∧ c = false ∧ d = false := by
  cases a <;> cases b <;> cases c <;> cases d <;> simp_all [route]

theorem excluded_never_routes (a b d : Bool) : route a b true d = 0 := by
  simp [route]

def bools := [false, true]
def policies := [Policy.excluded, Policy.cloud, Policy.sensitive]
def pnum : Policy → Nat | .excluded => 0 | .cloud => 1 | .sensitive => 2

def main (args : List String) : IO Unit := do
  match args with
  | [path] =>
    let reads := policies.flatMap fun p => bools.flatMap fun a => bools.flatMap fun b =>
      bools.flatMap fun c => bools.map fun d =>
        Lean.toJson (pnum p, [a,b,c,d], mayRead a b c d p)
    let ancestry := bools.flatMap fun a => bools.flatMap fun b => bools.flatMap fun c =>
      bools.flatMap fun d => bools.map fun e => Lean.toJson ([a,b,c,d,e], ancestor a b c d e)
    let names := tools ++ ["shell", "read_file", "web_fetch", "write_file", "spawn_agent", "", "unknown"]
    let capabilities := names.map fun name => Lean.toJson (name, toolAllowed name)
    let routes := bools.flatMap fun a => bools.flatMap fun b => bools.flatMap fun c =>
      bools.map fun d => Lean.toJson ([a,b,c,d], route a b c d)
    let result := Lean.Json.mkObj [("reads", Lean.toJson reads), ("ancestry", Lean.toJson ancestry),
                                  ("tools", Lean.toJson capabilities), ("routes", Lean.toJson routes)]
    IO.FS.writeFile ⟨path⟩ (result.compress ++ "\n")
  | _ => throw <| IO.userError "usage: search_fixtures OUTPUT_PATH"

end Search
def main := Search.main
