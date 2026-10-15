import Lake
open Lake DSL

package loomPermissionPolicy

lean_lib LoomPermissionPolicy

@[default_target]
lean_exe loom_permission_fixtures where
  root := `Main
