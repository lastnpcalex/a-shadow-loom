import LoomPermissionPolicy.Policy

open LoomPermissionPolicy

def profileName : Profile -> String
  | .strict => "strict"
  | .balanced => "balanced"
  | .trusted => "trusted"
  | .trustReview => "trust_review"

def effectName : Effect -> String
  | .read => "read"
  | .workspaceWrite => "workspace_write"
  | .execute => "execute"
  | .network => "network"
  | .externalMutation => "external_mutation"
  | .sensitiveRead => "sensitive_read"
  | .lifecycle => "lifecycle"
  | .unknown => "unknown"

def boundaryName : Boundary -> String
  | .workspace => "workspace"
  | .outsideWorkspace => "outside_workspace"
  | .external => "external"
  | .unknown => "unknown"

def decisionName : Decision -> String
  | .autoAllow => "auto_allow"
  | .ask => "ask"
  | .askWithWarning => "ask_with_warning"
  | .invalidRequest => "invalid_request"

def boolJson : Bool -> String
  | true => "true"
  | false => "false"

def profiles : List Profile := [.strict, .balanced, .trusted, .trustReview]

def effects : List Effect := [
  .read,
  .workspaceWrite,
  .execute,
  .network,
  .externalMutation,
  .sensitiveRead,
  .lifecycle,
  .unknown
]

def boundaries : List Boundary := [
  .workspace,
  .outsideWorkspace,
  .external,
  .unknown
]

def booleans : List Bool := [false, true]

def caseJson
    (profile : Profile)
    (effect : Effect)
    (boundary : Boundary)
    (valid warned ruleMatched destructive : Bool) : String :=
  let action : Action := {
    effect := effect
    boundary := boundary
    valid := valid
    warned := warned
    ruleMatched := ruleMatched
    destructive := destructive
  }
  "  {\"profile\":\"" ++ profileName profile ++
    "\",\"effect\":\"" ++ effectName effect ++
    "\",\"boundary\":\"" ++ boundaryName boundary ++
    "\",\"valid\":" ++ boolJson valid ++
    ",\"warned\":" ++ boolJson warned ++
    ",\"rule_matched\":" ++ boolJson ruleMatched ++
    ",\"destructive\":" ++ boolJson destructive ++
    ",\"decision\":\"" ++ decisionName (decide profile action) ++ "\"}"

def generatedCases : List String :=
  profiles.flatMap fun profile =>
    effects.flatMap fun effect =>
      boundaries.flatMap fun boundary =>
        booleans.flatMap fun valid =>
          booleans.flatMap fun warned =>
            booleans.flatMap fun ruleMatched =>
              booleans.map fun destructive =>
                caseJson profile effect boundary valid warned ruleMatched destructive

def main (args : List String) : IO Unit := do
  match args with
  | outputPath :: _ =>
      let payload := "[\n" ++ String.intercalate ",\n" generatedCases ++ "\n]\n"
      IO.FS.writeFile (System.FilePath.mk outputPath) payload
      IO.println s!"Wrote {generatedCases.length} permission cases to {outputPath}"
  | [] =>
      throw <| IO.userError "usage: loom_permission_fixtures OUTPUT_PATH"
