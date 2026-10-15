namespace LoomPermissionPolicy

inductive Profile where
  | strict
  | balanced
  | trusted
  | trustReview
  deriving Repr, DecidableEq, BEq

inductive Effect where
  | read
  | workspaceWrite
  | execute
  | network
  | externalMutation
  | sensitiveRead
  | lifecycle
  | unknown
  deriving Repr, DecidableEq, BEq

inductive Boundary where
  | workspace
  | outsideWorkspace
  | external
  | unknown
  deriving Repr, DecidableEq, BEq

inductive Decision where
  | autoAllow
  | ask
  | askWithWarning
  | invalidRequest
  deriving Repr, DecidableEq, BEq

structure Action where
  effect : Effect
  boundary : Boundary
  valid : Bool
  warned : Bool
  ruleMatched : Bool
  destructive : Bool := false
  deriving Repr, DecidableEq, BEq

structure RuleContext where
  sameWorkspace : Bool
  branchDescendant : Bool
  shapeMatches : Bool
  deriving Repr, DecidableEq, BEq

def ruleApplies (context : RuleContext) : Bool :=
  context.sameWorkspace && context.branchDescendant && context.shapeMatches

private def askDecision (warned : Bool) : Decision :=
  if warned then .askWithWarning else .ask

def decide (profile : Profile) (action : Action) : Decision :=
  if !action.valid then
    .invalidRequest
  else if profile == .trustReview then
    if action.destructive then .askWithWarning else .autoAllow
  else if action.ruleMatched then
    .autoAllow
  else if action.effect == .unknown || action.boundary == .unknown then
    askDecision action.warned
  else if action.boundary != .workspace then
    askDecision action.warned
  else
    match action.effect with
    | .network | .externalMutation | .sensitiveRead | .lifecycle =>
        askDecision action.warned
    | .read => .autoAllow
    | .workspaceWrite =>
        match profile with
        | .strict => askDecision action.warned
        | .balanced | .trusted | .trustReview => .autoAllow
    | .execute =>
        match profile with
        | .trusted | .trustReview => .autoAllow
        | .strict | .balanced => askDecision action.warned
    | .unknown => askDecision action.warned

inductive ResolutionSource where
  | user
  | loomPolicy
  | loomLifecycle
  | loomTimeout
  | bridgeError
  deriving Repr, DecidableEq, BEq

inductive Resolution where
  | allowed
  | userDenied
  | policyBlocked
  | cancelled
  deriving Repr, DecidableEq, BEq

def resolve (allowed : Bool) (source : ResolutionSource) : Resolution :=
  if allowed then
    .allowed
  else
    match source with
    | .user => .userDenied
    | .loomPolicy => .policyBlocked
    | .loomLifecycle | .loomTimeout | .bridgeError => .cancelled

def permissiveness : Decision -> Nat
  | .invalidRequest => 0
  | .ask | .askWithWarning => 1
  | .autoAllow => 2

theorem only_user_source_can_deny (allowed : Bool) (source : ResolutionSource) :
    resolve allowed source = .userDenied <->
      (allowed = false /\ source = .user) := by
  cases allowed <;> cases source <;> decide

theorem scoped_rule_requires_every_constraint (context : RuleContext) :
    ruleApplies context = true <->
      (context.sameWorkspace = true /\
       context.branchDescendant = true /\
       context.shapeMatches = true) := by
  cases context with
  | mk sameWorkspace branchDescendant shapeMatches =>
      cases sameWorkspace <;> cases branchDescendant <;> cases shapeMatches <;>
        decide

theorem strict_no_more_permissive_than_balanced (action : Action) :
    permissiveness (decide .strict action) <=
      permissiveness (decide .balanced action) := by
  cases action with
  | mk effect boundary valid warned ruleMatched destructive =>
      cases effect <;> cases boundary <;> cases valid <;> cases warned <;>
        cases ruleMatched <;> cases destructive <;> decide

theorem balanced_no_more_permissive_than_trusted (action : Action) :
    permissiveness (decide .balanced action) <=
      permissiveness (decide .trusted action) := by
  cases action with
  | mk effect boundary valid warned ruleMatched destructive =>
      cases effect <;> cases boundary <;> cases valid <;> cases warned <;>
        cases ruleMatched <;> cases destructive <;> decide

theorem warning_never_revokes_auto_allow
    (profile : Profile) (effect : Effect) (boundary : Boundary)
    (valid ruleMatched destructive : Bool) :
    decide profile {
      effect := effect
      boundary := boundary
      valid := valid
      warned := false
      ruleMatched := ruleMatched
      destructive := destructive
    } = .autoAllow ->
    decide profile {
      effect := effect
      boundary := boundary
      valid := valid
      warned := true
      ruleMatched := ruleMatched
      destructive := destructive
    } = .autoAllow := by
  cases profile <;> cases effect <;> cases boundary <;> cases valid <;>
    cases ruleMatched <;> cases destructive <;> decide

theorem trust_review_destructive_always_asks
    (effect : Effect) (boundary : Boundary) (warned ruleMatched : Bool) :
    decide .trustReview {
      effect := effect
      boundary := boundary
      valid := true
      warned := warned
      ruleMatched := ruleMatched
      destructive := true
    } = .askWithWarning := by
  rfl

theorem trust_review_other_valid_actions_allow
    (effect : Effect) (boundary : Boundary) (warned ruleMatched : Bool) :
    decide .trustReview {
      effect := effect
      boundary := boundary
      valid := true
      warned := warned
      ruleMatched := ruleMatched
      destructive := false
    } = .autoAllow := by
  rfl

theorem legacy_profiles_ignore_destructive_signal (profile : Profile) (action : Action)
    (legacy : profile ≠ .trustReview) :
    decide profile {action with destructive := true} =
      decide profile {action with destructive := false} := by
  cases profile <;> first | exact False.elim (legacy rfl) | rfl

theorem same_normalized_action_same_decision
    (profile : Profile) (action : Action) :
    decide profile action = decide profile action := by
  rfl

end LoomPermissionPolicy
