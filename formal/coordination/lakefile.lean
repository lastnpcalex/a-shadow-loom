import Lake
open Lake DSL
package loomCoordination
lean_lib LoomCoordination
@[default_target]
lean_exe coordination_fixtures where
  root := `Main
@[default_target]
lean_exe recovery_fixtures where
  root := `Recovery
